import asyncio
import contextlib
import json
import math
import time
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    FrozenSet,
    List,
    Literal,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

import aiofiles
import httpx
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from pydantic import BaseModel

from nekro_agent.core import config, logger
from nekro_agent.schemas.usage import LLMUsageStatus
from nekro_agent.services import embedding_health
from nekro_agent.services.usage.context import current_usage_context
from nekro_agent.services.usage.ledger import compute_token_cost, record_llm_usage

from . import gemini_native
from .creator import OpenAIChatMessage
from .upstream_redaction import (
    collect_llm_secret_values,
    format_redacted_traceback,
    redact_upstream_text,
)

_OPENAI_BASE_URL = "https://api.openai.com/v1"

_T = TypeVar("_T")

# 当前请求属于哪个频道。提示词日志在 `save_log` 内部落盘，而那个函数不能改签名，
# 所以用 ContextVar 把 chat_key 带进去；默认空串表示不做额外替换。
_LOG_PENDING_CHAT_KEY: ContextVar[str] = ContextVar("llm_log_pending_chat_key", default="")


def set_log_pending_chat_key(chat_key: str) -> Any:
    """在发请求前登记当前频道的 chat_key，返回一个供复位用的 token"""
    return _LOG_PENDING_CHAT_KEY.set(str(chat_key or ""))


def reset_log_pending_chat_key(token: Any) -> None:
    """请求结束后复位，避免同一任务里的后续调用误用上一个频道的片段"""
    _LOG_PENDING_CHAT_KEY.reset(token)


@contextlib.contextmanager
def log_pending_chat_key(chat_key: str):
    """请求期间把 chat_key 带给 `save_log`，退出时一定复位"""
    token = set_log_pending_chat_key(chat_key)
    try:
        yield
    finally:
        reset_log_pending_chat_key(token)


def _note_prompt_log(chat_key: str, path: Path) -> None:
    """记下这个提示词日志文件属于哪个频道：聊天登记服务器后的原文清理只碰本频道的文件"""
    if not chat_key:
        return
    try:
        from nekro_agent.services.ssh_gateway.scrub import note_prompt_log

        note_prompt_log(chat_key, path)
    except Exception:  # noqa: BLE001 - 记不下来只影响之后的清理范围，不影响日志本身
        pass


class EmptyLLMResponseError(ValueError):
    """上游返回了 2xx，但正文与思考通道里都没有任何可用内容

    与「请求失败」区分开是有意义的：这一类**已经计过费**（实测有中转站在
    `completion_tokens=0` 而 `reasoning_tokens=1116` 的情况下照样计费），
    重试有意义但换模型未必有意义，而且它在日志与聊天回执里都该是可辨识的一类，
    不能和网络错误混成同一句「与 LLM 通信出错」。
    """


# 「这条回复被上游挡下来了」的 finish_reason 取值集合。
#
# 取值来自 Gemini 的 `FinishReason` 枚举（`PROHIBITED_CONTENT` / `SAFETY` / `RECITATION` /
# `BLOCKLIST` / `SPII` / `IMAGE_SAFETY`）与 OpenAI 自己的 `content_filter`。现场 733 份
# `prompts_error/*.json` 里 `prohibited_content` 有 26 条，全部伴随 `content=None`——
# 也就是说安全拦截**当前**是走「正文为空」这一支的，用户只会看到一句「一个字都没返回」，
# 而真实原因（换个说法就能过）一个字都传达不出去。
CONTENT_BLOCKED_FINISH_REASONS: FrozenSet[str] = frozenset(
    {
        "prohibited_content",
        "content_filter",
        "safety",
        "recitation",
        "blocklist",
        "spii",
        "image_safety",
    },
)

# 模型把工具调用写坏了（Gemini `MALFORMED_FUNCTION_CALL`，现场 13 条）。
MALFORMED_FUNCTION_CALL_FINISH_REASON = "malformed_function_call"


def normalize_finish_reason(raw: Any) -> str:
    """把上游的 finish_reason 归一成小写下划线形式

    同一个原因各家写法不一样（原生 Gemini 回 `PROHIBITED_CONTENT`，兼容层通常已转小写，
    也见过中转站写成 `content-filter`）。判据必须先归一，否则「换个写法就漏判」，
    而漏判的后果正是这次要修的那一条：拒答被当成普通空响应。
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower().replace("-", "_")


def _choice_finish_reason(choice: Any) -> str:
    """从一个 choice 上取归一化的 finish_reason

    用 `getattr` 而不是直接取属性：流式的中间块、以及部分中转站的响应体里这个字段会整个缺席，
    直接取属性会把一次「本可分类的失败」变成一条 AttributeError。
    """
    return normalize_finish_reason(getattr(choice, "finish_reason", None))


_CACHED_INPUT_TOP_LEVEL_FIELDS = (
    "prompt_cache_hit_tokens",  # DeepSeek
    "cache_read_input_tokens",  # 转发 Anthropic 的兼容接口
    "cached_tokens",  # Moonshot 等
    "cached_content_token_count",  # Gemini 原生（SDK 属性名）
    "cachedContentTokenCount",  # Gemini 原生（REST 字段名）
)


def _cached_input_tokens(usage: Any) -> Optional[int]:
    """从上游 `usage` 里取「输入命中缓存的 token 数」，一个都取不到时回 `None`

    各家放这个数字的位置不一样，下面几处都看，取**各处合法值里最大的**：中转站常见
    `prompt_tokens_details.cached_tokens=0` 同时带 `prompt_cache_hit_tokens=500`，
    取第一个就会把真实命中记成 0。

    - `prompt_tokens_details.cached_tokens`（OpenAI、Gemini 兼容接口等）
    - 顶层 `prompt_cache_hit_tokens` / `cache_read_input_tokens` / `cached_tokens` /
      `cached_content_token_count`（`cachedContentTokenCount`），见
      :data:`_CACHED_INPUT_TOP_LEVEL_FIELDS`

    `usage` 可能是 openai SDK 的 pydantic 对象（非标准字段落在 `model_extra`），也可能是 dict。
    合法值是 `int` 或数值为整数的 `float`，且 `>= 0`；`bool`、字符串、负数、NaN 与其他类型都
    当没有这个字段。

    回 `None` 表示「这份 usage 没说」：流式时调用方据此保留前面块里已经读到的数，
    不被后面一个只带 token 总数的块清零；非流式按 0 记。

    Anthropic 语义下缓存读取不算在输入 token 里，转发时如果 `prompt_tokens` 只是未命中的
    部分，这里读到的数会大于它，记账时被夹到等于输入数（命中率显示 100%）。

    整个函数兜底回 `None` 且不打日志：它每次请求都会走，服务商不回这个字段或回了怪东西时
    既不能报错，也不能刷屏影响记账。
    """
    try:

        def _valid(value: Any) -> Optional[int]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
                return None
            number = int(value)
            return number if number >= 0 else None

        def _top_level(name: str) -> Any:
            if usage is None:
                return None
            if isinstance(usage, dict):
                return usage.get(name)
            value = getattr(usage, name, None)
            if value is not None:
                return value
            extra = getattr(usage, "model_extra", None)
            return extra.get(name) if isinstance(extra, dict) else None

        if usage is None:
            return None
        candidates: List[int] = []
        details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(
            usage,
            "prompt_tokens_details",
            None,
        )
        if details is not None:
            cached = details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", None)
            valid = _valid(cached)
            if valid is not None:
                candidates.append(valid)

        for name in _CACHED_INPUT_TOP_LEVEL_FIELDS:
            valid = _valid(_top_level(name))
            if valid is not None:
                candidates.append(valid)
        return max(candidates) if candidates else None
    except Exception:  # noqa: BLE001 - 记账不能因为上游字段形状而失败
        return None


class LLMContentBlockedError(EmptyLLMResponseError):
    """上游以「内容被安全/策略拦截」收尾，正文因此为空

    **故意继承 `EmptyLLMResponseError`**：它在链路上确实是「2xx 但没有可用正文」的一个子集，
    既有那些 `except EmptyLLMResponseError` 的调用点一行都不用改也不会漏接——本项目栽过多次的
    「只修一半」在这里被继承关系直接堵死。

    分出子类是因为**下一步动作不同**：安全拦截是上游对内容本身的判决，原样重试必然再被拦一次，
    用户需要的是「换个说法」而不是「再等一会儿」。

    `finish_reason` 只保留归一化后的取值，且必然属于 `CONTENT_BLOCKED_FINISH_REASONS`
    这个固定集合，不含任何上游正文——与 `failure_feedback` 那条「文案取自固定集合」同一约束。
    """

    def __init__(self, finish_reason: str, *, stream_mode: bool) -> None:
        self.finish_reason = finish_reason
        super().__init__(
            f"{'流式' if stream_mode else '非流式'}响应被上游内容策略拦截"
            f"（finish_reason={finish_reason}），未返回任何可用正文",
        )


class LLMMalformedToolCallError(EmptyLLMResponseError):
    """模型把工具调用写坏了（`finish_reason=malformed_function_call`），正文因此为空

    为什么**不**并进 `LLMContentBlockedError`：两者只有「正文为空」这个表象相同，下一步动作正相反。
    安全拦截是对内容的确定性判决，同一段输入重发多少次都还是被拦；写坏的工具调用是一次采样事故，
    重发大概率就好了。并成一类的代价二选一——要么把可重试的这一类也判成不必重试，
    要么给用户一句「内容被挡下来了」的错误归因。

    为什么也**没有**为它另立 `LLMFailureKind`：它对上层的动作与 `EMPTY_RESPONSE` 完全一致
    （重试有意义、文案就是「一个字都没返回，再说一次」），另立一个没有任何分支会读的枚举值
    只会让契约变宽而不变强。单独一个异常类的价值在可观测性：错误日志里的 `error.type`
    能把这 13 条继续单独数出来，而不是重新混进 511 条 `EmptyLLMResponseError` 里。
    """

    def __init__(self, *, stream_mode: bool) -> None:
        self.finish_reason = MALFORMED_FUNCTION_CALL_FINISH_REASON
        super().__init__(
            f"{'流式' if stream_mode else '非流式'}响应以 "
            f"{MALFORMED_FUNCTION_CALL_FINISH_REASON} 收尾（模型把工具调用写坏了），未返回任何可用正文",
        )


def _salvage_reasoning_only_output(
    output: str,
    thought_chain: str,
    *,
    model: str,
    field_name: str,
) -> str:
    """正文为空但思考通道有内容时，把思考通道的内容当作正文

    这是**我们这一侧的缺陷**而不是供应商行为：原生思考模型（实测 `deepseek-v4-pro`）在
    `ENABLE_COT` 开着、被要求输出 `<think>` 格式时，会把整段输出——包含收尾的
    ```python 代码块——全部走 `reasoning_content` 通道，`content` 留空。此时
    `finish_reason='stop'`、`completion_tokens` 是几百，答案完全可解析可执行，
    只是被判空丢弃了。取回来即可，`resolver.parse_chat_response` 对这段内容的解析
    与正常正文没有区别。

    注意与「真的一个字都没返回」区分：那一类 `content` 与 `reasoning_content` 同为空，
    走不到这里，会被判成 `EmptyLLMResponseError`。
    """
    if output.strip() or not thought_chain.strip():
        return output
    logger.warning(
        f"模型 {model} 的正文为空但 {field_name} 有内容（原生思考模型把整段输出走了思考通道），"
        f"已回退使用思考通道内容作为正文 | 长度: {len(thought_chain)}",
    )
    return thought_chain


def parse_extra_body(extra_body_json: Optional[str], source_hint: str = "") -> Optional[Dict[str, Any]]:
    """解析 extra_body JSON 字符串

    Args:
        extra_body_json: JSON 字符串
        source_hint: 来源提示，用于日志记录

    Returns:
        解析后的字典，如果解析失败或输入为空则返回 None
    """
    if not extra_body_json:
        return None
    try:
        return json.loads(extra_body_json)
    except Exception as e:
        truncated_body = extra_body_json[:100] + "..." if len(extra_body_json) > 100 else extra_body_json
        # `EXTRA_BODY` 是模型组的自由文本字段，某些上游要求把凭据写在请求体里
        # （`{"api_key": "..."}` / 自建网关的签名串）。解析失败时把原文打进日志，
        # 等于把这类凭据落盘，因此这一行的两个插值都要过脱敏
        secret_values = collect_llm_secret_values()
        logger.error(
            f"解析 EXTRA_BODY 失败 ({source_hint}): {redact_upstream_text(str(e), secret_values)} "
            f"| Original: {redact_upstream_text(truncated_body, secret_values)}",
        )
        return None


class OpenAIResponse(BaseModel):
    response_content: str  # 最终的回复内容
    thought_chain: str  # 思考链
    messages: List[Dict[str, Any]]  # 原始消息列表
    message_cnt: int  # 消息数量
    token_consumption: int  # 总 token 消耗
    token_input: int  # 输入 token 消耗
    token_output: int  # 输出 token 消耗
    use_model: str  # 使用的模型
    speed_tokens_per_second: float  # token 生成速度 / 秒
    first_token_cost_ms: int  # 首 token 生成时间
    generation_time_ms: int  # 总生成时间
    stream_mode: bool  # 是否为流式模式
    log_path: Optional[Union[str, Path]] = None  # 日志文件路径

    def gen_log(self, lang: str = "zh") -> str:
        """生成日志"""
        if lang == "zh":
            return (
                f"[OpenAI{'-Stream' if self.stream_mode else ''}] 使用模型: {self.use_model}\n"
                f"总 token 消耗: {self.token_consumption}\n"
                f"输入 token: {self.token_input}\n"
                f"输出 token: {self.token_output}\n"
                f"总生成时间: {self.generation_time_ms}ms\n"
                f"生成速度: {self.speed_tokens_per_second} tokens/s\n"
                f"首 token 生成时间: {self.first_token_cost_ms}ms\n"
            )
        return (
            f"[OpenAI{'-Stream' if self.stream_mode else ''}] Use model: {self.use_model}\n"
            f"Total token consumption: {self.token_consumption}\n"
            f"Input token: {self.token_input}\n"
            f"Output token: {self.token_output}\n"
            f"Total generation time: {self.generation_time_ms}ms\n"
            f"Token generation speed: {self.speed_tokens_per_second} tokens/s\n"
            f"First token cost: {self.first_token_cost_ms}ms\n"
        )

    def price_consumption(self, base_rate: float = 1, completion_rate: float = 1, token_price: float = 1) -> float:
        """计算价格

        公式已抽到 `services/usage/ledger.compute_token_cost`，本方法只是它的一层薄封装：
        失败的那些请求根本构造不出 `OpenAIResponse`，而它们同样要落账，于是两处各写一份
        「怎么算钱」就是本项目栽过六次的那个形态（同一判据的第二处实现）。
        """
        return compute_token_cost(
            self.token_input,
            self.token_output,
            input_rate=base_rate,
            completion_rate=completion_rate,
            price_rate=token_price,
        )

    def _generate_json_log(
        self,
        messages: Any,
        message_cnt: int,
        temperature: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop_words: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """生成JSON格式的日志内容"""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "model": self.use_model,
            "stream_mode": self.stream_mode,
            "metrics": {
                "token_consumption": self.token_consumption,
                "token_input": self.token_input,
                "token_output": self.token_output,
                "generation_time_ms": self.generation_time_ms,
                "speed_tokens_per_second": self.speed_tokens_per_second,
                "first_token_cost_ms": self.first_token_cost_ms,
                "message_cnt": message_cnt,
            },
            "request": {
                "messages": messages,
                "temperature": temperature,
                "frequency_penalty": frequency_penalty,
                "presence_penalty": presence_penalty,
                "top_p": top_p,
                "max_tokens": max_tokens,
                "stop_words": stop_words,
            },
            "response": {"content": self.response_content, "thought_chain": self.thought_chain},
        }
        error_section = self._log_error_section()
        if error_section is not None:
            log_data["error"] = error_section
        return log_data

    def _log_error_section(self) -> Optional[Dict[str, Any]]:
        """错误落盘日志里的「响应侧原因」小节；成功响应没有这一节"""
        return None

    def _generate_text_log(
        self,
        messages: Any,
        message_cnt: int,
        temperature: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """生成文本格式的日志内容"""
        log_content = [
            f"--- LLM调用日志 [{datetime.now().isoformat()}] ---",
            f"模型: {self.use_model} {'(流式)' if self.stream_mode else ''}",
            f"消息数量: {message_cnt}",
            f"Token消耗: 总计={self.token_consumption}, 输入={self.token_input}, 输出={self.token_output}",
            f"生成时间: {self.generation_time_ms}ms, 首token时间: {self.first_token_cost_ms}ms",
            f"生成速度: {self.speed_tokens_per_second:.2f} tokens/s",
            f"生成参数: temp={temperature}, freq_p={frequency_penalty}, pres_p={presence_penalty}, top_p={top_p}, max_tokens={max_tokens}",
            "-----------------------------------\n",
            "请求详情:",
        ]

        # 添加请求消息内容，使用<|role|>格式
        for _, msg in enumerate(messages):
            try:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")

                # 处理混合 content 类型
                if isinstance(content, list):
                    processed_content = []
                    for item in content:
                        item_type = item.get("type", "unknown")

                        if item_type == "text":
                            # 文本类型，直接添加内容
                            text_content = item.get("text", "")
                            processed_content.append(text_content)

                        elif item_type == "image_url":
                            # 图片URL类型，简化显示
                            image_url = item.get("image_url", {}).get("url", "")
                            if len(image_url) > 128:
                                # 截取前60个和后60个字符，中间用...连接
                                truncated_url = f"{image_url[:60]}...{image_url[-60:]}"
                                processed_content.append(f"[图片] {truncated_url}")
                            else:
                                processed_content.append(f"[图片] {image_url}")

                        else:
                            # 其他类型
                            processed_content.append(f"[{item_type}] {str(item)[:100]}...")

                    # 合并处理后的内容
                    log_content.append(f"<|{role}|>\n" + "\n".join(processed_content) + "\n")
                else:
                    # 处理普通字符串内容
                    log_content.append(f"<|{role}|>\n{content}\n")
            except Exception as e:
                log_content.append(f"<|unknown|>\n处理消息时出错: {str(e)[:100]}...\n原始消息: {str(msg)[:100]}...")

        log_content.append("\n-----------------------------------\n")

        # 添加响应内容
        log_content.extend(
            [
                "思考链:",
                f"{self.thought_chain or '<Empty>'}",
                "-----------------------------------\n",
                "响应内容:",
                f"<|assistant|>\n{self.response_content or '<Empty>'}",
                "-----------------------------------\n",
            ],
        )

        error_section = self._log_error_section()
        if error_section is not None:
            log_content.extend(
                [
                    "失败原因:",
                    f"{error_section.get('type', '')}: {error_section.get('message', '')}",
                    "-----------------------------------\n",
                ],
            )

        return "\n".join(log_content)

    async def save_log(
        self,
        log_path: Union[str, Path],
        log_style: Literal["json", "text", "auto"] = "auto",
        messages: Any = None,
        message_cnt: int = 0,
        temperature: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop_words: Optional[List[str]] = None,
    ) -> bool:
        """保存日志到文件"""
        if not log_path:
            return False

        path: Path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if log_style == "auto":
            log_style = "json" if path.suffix == ".json" else "text"

        pending_chat_key = _LOG_PENDING_CHAT_KEY.get()

        if log_style == "json":
            log_data = self._generate_json_log(
                messages,
                message_cnt,
                temperature,
                frequency_penalty,
                presence_penalty,
                top_p,
                max_tokens,
                stop_words,
            )
            payload = (
                json.dumps(
                    log_data,
                    ensure_ascii=False,
                    default=lambda o: str(o) if not isinstance(o, (dict, list, str, int, float, bool, type(None))) else o,
                )
                + "\n"
            )
            if pending_chat_key:
                from nekro_agent.services.ssh_gateway.scrub import apply_pending

                payload = apply_pending(pending_chat_key, payload)
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                await f.write(payload)
            _note_prompt_log(pending_chat_key, path)
        else:
            log_text = self._generate_text_log(
                messages,
                message_cnt,
                temperature,
                frequency_penalty,
                presence_penalty,
                top_p,
                max_tokens,
            )
            if pending_chat_key:
                from nekro_agent.services.ssh_gateway.scrub import apply_pending

                log_text = apply_pending(pending_chat_key, log_text)
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                await f.write(log_text)
            _note_prompt_log(pending_chat_key, path)

        return True


class OpenAIErrResponse(OpenAIResponse):
    """失败请求的落盘形态

    `error_msg` / `error_type` 必须真的写进日志文件：改造前 `save_log` 走的是父类的
    `_generate_json_log`，那个方法只序列化 `request` 与一份全零的 `metrics`，
    于是 `prompts_error/*.json` 里**一个字的失败原因都没有**——1.4MB 的文件里全是图片
    base64，真正的原因只在 app.log 的一行 `logger.exception` 里。排查时这两处对不上号
    （落盘文件按模型名+时间戳命名，而那个模型名当时还是错的，见 `run_agent`）。
    """

    error_msg: str  # 错误信息（**已脱敏**，见 create_from_exception）
    error_type: str = ""  # 异常类名，便于分类统计

    def _log_error_section(self) -> Optional[Dict[str, Any]]:
        return {"type": self.error_type, "message": self.error_msg}

    @classmethod
    def create_from_exception(
        cls,
        e: Exception,
        use_model: str,
        stream_mode: bool,
        messages: List[Dict[str, Any]],
        log_path: Optional[Union[str, Path]] = None,
        secret_values: Optional[FrozenSet[str]] = None,
    ) -> "OpenAIErrResponse":
        """脱敏在**这里**做，不在调用点

        `error_msg` 的唯一去向就是落盘（`prompts_error/*.json` 的 `error.message` 与文本日志
        的「失败原因」小节），而这些文件会进 `/deployment/backup` 的打包。把脱敏放在构造函数里，
        意味着「这个类的实例天生不带凭据」——将来多一个调用点也不会多一处漏洞，
        而放在调用点则要求每个调用者都记得（以前正是有调用点忘了脱敏才漏的）。

        Args:
            secret_values: 本次请求的字面量凭据集合；省略时现场收集
        """
        return cls(
            error_type=type(e).__name__,
            response_content="",
            thought_chain="",
            messages=messages,
            message_cnt=0,
            token_consumption=0,
            token_input=0,
            token_output=0,
            use_model=use_model,
            speed_tokens_per_second=0,
            first_token_cost_ms=0,
            generation_time_ms=0,
            stream_mode=stream_mode,
            log_path=log_path,
            error_msg=redact_upstream_text(str(e), secret_values),
        )


class OpenAIStreamChunk(BaseModel):
    chunk_text: str  # 当前生成的文本
    thought_chain: str  # 当前的思考链
    token_consumption: int  # 当前token消耗
    token_input: int  # 当前输入token消耗
    token_output: int  # 当前输出token消耗


_AsyncFunc = Callable[..., Coroutine[Any, Any, OpenAIStreamChunk]]


# 连接池参数：LLM 调用是长连接短并发的形态，keepalive 存活时间取得比 httpx 默认的
# 5 秒长得多，才能让相邻两次请求（例如一次沉淀里的 1 次 chat + N 次 embedding）真正复用连接
_HTTP_CLIENT_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=300.0,
)

# 缓存键：(base_url, proxy_url, connect, read, write, pool)
_HttpClientKey = Tuple[str, str, int, int, int, int, bool]


class _PooledHttpClient(NamedTuple):
    """缓存的 HTTP 客户端及其所属事件循环"""

    client: httpx.AsyncClient
    loop: asyncio.AbstractEventLoop


# 缓存键的每一个分量都来自模型组（base_url / 代理 / 超时）。多租户下组由租户建、地址由租户填，
# 拉模型列表的地址甚至直接来自请求体，条目数不再被配置规模天然限住，所以按最近使用淘汰，最多留这么多个
_HTTP_CLIENT_CACHE_MAX = 64
_http_clients: "OrderedDict[_HttpClientKey, _PooledHttpClient]" = OrderedDict()
# 被挤出缓存、还有在途请求的客户端（按 id(client)）。不能立刻关：关掉会把在途请求打断，而流式回复在首个 token
# 之后没有总时长上限（httpx 的读超时按每次读算），按「各项超时之和」估时长靠不住。所以给在途请求计数
# （`_TrackedAsyncClient`），等最后一个请求结束（流式的是流被关掉）再关
_retiring_clients: Dict[int, _PooledHttpClient] = {}
_closing_tasks: Set["asyncio.Task[None]"] = set()
# 被挤出时、或最后一个在途请求结束时没有在途请求，也不立刻关：`_get_http_client` 交出客户端之后，调用方
# （openai SDK）发请求之前还有一段空档（中间有线程切换），这时关掉，那次请求就报「客户端已关闭」。
# 宽限这么久再看一次在途数，还是 0 才关
_RETIRE_GRACE_SECONDS = 30.0


async def _aclose_quietly(client: httpx.AsyncClient) -> None:
    with contextlib.suppress(Exception):
        await client.aclose()


class _ReleasingStream(httpx.AsyncByteStream):
    """响应体关掉时通知一次（流式回复读完、出错或被调用方提前关掉都算）"""

    def __init__(self, inner: httpx.AsyncByteStream, release: Callable[[], None]):
        self._inner = inner
        self._release = release

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._release()


class _TrackedAsyncClient(httpx.AsyncClient):
    """记着在途请求数的客户端：被挤出缓存之后，等它空闲了再关"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.na_in_flight = 0
        self.na_retired_on: Optional[asyncio.AbstractEventLoop] = None
        self.na_close_handle: Optional[asyncio.TimerHandle] = None

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        self.na_in_flight += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self.na_in_flight -= 1
            if self.na_retired_on is not None and self.na_in_flight == 0:
                _schedule_retired_close(self)

        try:
            response = await super().send(request, **kwargs)
        except BaseException:
            release()
            raise
        if response.is_closed:  # 非流式：正文已经读完、响应已关
            release()
        else:
            response.stream = _ReleasingStream(response.stream, release)  # type: ignore[arg-type]
        return response


def _close_retired_client(client: "_TrackedAsyncClient") -> None:
    loop = client.na_retired_on
    _retiring_clients.pop(id(client), None)
    if client.na_close_handle is not None:
        client.na_close_handle.cancel()
        client.na_close_handle = None
    if loop is None or loop.is_closed():
        return  # 循环已结束，没法在它上面关，交给 GC（与覆盖旧循环条目同一做法）
    task = loop.create_task(_aclose_quietly(client))
    _closing_tasks.add(task)
    task.add_done_callback(_closing_tasks.discard)


def _schedule_retired_close(client: "_TrackedAsyncClient") -> None:
    """宽限 `_RETIRE_GRACE_SECONDS` 再看一次：还是没有在途请求才关；有的话等最后一个结束时再排一次"""
    loop = client.na_retired_on
    if loop is None or loop.is_closed():
        _retiring_clients.pop(id(client), None)
        return  # 交给 GC
    if client.na_close_handle is not None:
        return  # 已经排着了

    def check() -> None:
        client.na_close_handle = None
        if client.na_in_flight == 0:
            _close_retired_client(client)

    client.na_close_handle = loop.call_later(_RETIRE_GRACE_SECONDS, check)


def _retire_http_client(key: _HttpClientKey, pooled: _PooledHttpClient) -> None:
    """被挤出缓存的客户端：等在途请求都结束、再宽限一段（交出了还没发请求的那段空档）之后再关"""
    client = pooled.client
    if not isinstance(client, _TrackedAsyncClient):  # pragma: no cover - 缓存里只放 _TrackedAsyncClient
        return
    client.na_retired_on = pooled.loop
    _retiring_clients[id(client)] = pooled
    if client.na_in_flight == 0:
        _schedule_retired_close(client)


def _create_http_client(
    proxy_url: Optional[str] = None,
    read_timeout: int = 3600,
    write_timeout: int = 3600,
    connect_timeout: int = 10,
    pool_timeout: int = 10,
    tenant_egress: bool = False,
    client_class: type = httpx.AsyncClient,
) -> httpx.AsyncClient:
    """创建配置好的 httpx.AsyncClient

    统一管理 HTTP 客户端配置，包括：
    - User-Agent 强制设置为 config.OPENAI_CLIENT_USER_AGENT（通过事件钩子防止被覆盖）
    - 代理配置
    - 超时配置
    - `tenant_egress=True`（租户建的模型组）：走 `tools/tenant_egress.py`——每条连接解析一次、全部结果过校验
      （拒内网）、直连校验过的地址；组上填的代理同样是租户的，连它时一样处理。
      平台方的组不变

    返回的是一个全新的、调用方自行负责关闭的客户端。需要复用连接池的调用方应改用
    `_get_http_client`。

    Args:
        proxy_url: 代理 URL，如果提供则配置代理
        read_timeout: 读取超时时间（秒），默认 3600
        write_timeout: 写入超时时间（秒），默认 3600
        connect_timeout: 连接超时时间（秒），默认 10
        pool_timeout: 连接池超时时间（秒），默认 10

    Returns:
        配置好的 httpx.AsyncClient 实例
    """

    # 使用事件钩子强制设置 User-Agent，防止 AsyncOpenAI 覆盖。
    # 钩子在每次请求时读取配置，因此 UA 配置变更无需重建客户端即可生效
    async def enforce_user_agent(request: httpx.Request) -> None:
        request.headers["User-Agent"] = config.OPENAI_CLIENT_USER_AGENT

    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    )
    if tenant_egress:
        from nekro_agent.tools.tenant_egress import tenant_http_client

        # 代理语义照旧：组上填了代理就用它（它也是租户填的），没填按环境变量（部署方的）
        return tenant_http_client(
            proxy=proxy_url or None,
            proxy_is_tenant=True,
            trust_env=True,
            limits=_HTTP_CLIENT_LIMITS,
            subject="模型组接口地址",
            client_class=client_class,
            timeout=timeout,
            event_hooks={"request": [enforce_user_agent]},
        )
    return client_class(
        timeout=timeout,
        proxies={"http://": proxy_url, "https://": proxy_url} if proxy_url else None,
        limits=_HTTP_CLIENT_LIMITS,
        event_hooks={"request": [enforce_user_agent]},
    )


def _get_http_client(
    base_url: Optional[str] = None,
    proxy_url: Optional[str] = None,
    read_timeout: int = 3600,
    write_timeout: int = 3600,
    connect_timeout: int = 10,
    pool_timeout: int = 10,
    tenant_egress: bool = False,
) -> httpx.AsyncClient:
    """获取按连接参数分组复用的 httpx.AsyncClient

    每次请求新建并销毁客户端会让 TCP + TLS 握手无法摊销，keep-alive 完全失效；
    本函数按 (base_url, 代理, 各项超时) 缓存客户端，使相同上游的连续请求共用连接池。

    缓存键覆盖了全部会改变客户端行为的参数，因此配置变更（例如换代理、改超时）
    会自然落到新的键上并重建客户端，无需额外的失效通知；User-Agent 由请求事件钩子
    在每次请求时读取配置，同样天然跟随配置。需要强制回收全部连接时调用
    `reset_http_clients()`。

    返回的客户端由本模块持有，调用方**不得**关闭它，也不得交给会自动关闭它的上下文
    管理器（例如 `async with AsyncOpenAI(...)`）托管。

    Args:
        base_url: 上游 API 基础地址，仅用于隔离不同上游的客户端状态
        proxy_url: 代理 URL，如果提供则配置代理
        read_timeout: 读取超时时间（秒），默认 3600
        write_timeout: 写入超时时间（秒），默认 3600
        connect_timeout: 连接超时时间（秒），默认 10
        pool_timeout: 连接池超时时间（秒），默认 10

    Returns:
        可复用的 httpx.AsyncClient 实例
    """
    key: _HttpClientKey = (
        (base_url or _OPENAI_BASE_URL).rstrip("/"),
        proxy_url or "",
        connect_timeout,
        read_timeout,
        write_timeout,
        pool_timeout,
        # 租户的组与平台方的组即使地址相同也不共用连接池：两边连接的建法不一样
        bool(tenant_egress),
    )
    loop = asyncio.get_running_loop()

    pooled = _http_clients.get(key)
    if pooled is not None and pooled.loop is loop and not pooled.client.is_closed:
        _http_clients.move_to_end(key)
        return pooled.client

    client = _create_http_client(
        proxy_url=proxy_url,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
        connect_timeout=connect_timeout,
        pool_timeout=pool_timeout,
        tenant_egress=tenant_egress,
        client_class=_TrackedAsyncClient,
    )
    # 从查表到写回之间没有 await，因此对事件循环而言是原子的，无需额外加锁。
    # 若原条目绑定在已结束的事件循环上（例如每个用例自建循环的测试场景），无法在当前循环
    # 安全关闭它，直接覆盖交给 GC
    _http_clients[key] = _PooledHttpClient(client=client, loop=loop)
    _http_clients.move_to_end(key)
    while len(_http_clients) > _HTTP_CLIENT_CACHE_MAX:
        evicted_key, evicted = _http_clients.popitem(last=False)
        _retire_http_client(evicted_key, evicted)
    logger.debug(f"创建复用 HTTP 客户端: base_url={key[0]} | proxy={'on' if proxy_url else 'off'}")

    return client


async def reset_http_clients() -> None:
    """关闭并清空所有复用中的 HTTP 客户端

    供进程退出，以及需要强制断开全部上游连接的配置变更场景调用。下一次请求会按新配置
    重新建立客户端，因此调用本函数不会影响可用性。
    """
    pooled_clients = list(_http_clients.values())
    _http_clients.clear()
    # 已被挤出缓存、还在等在途请求结束或宽限期过去的也一并关掉
    for retiring in _retiring_clients.values():
        handle = getattr(retiring.client, "na_close_handle", None)
        if handle is not None:
            handle.cancel()
            retiring.client.na_close_handle = None  # type: ignore[attr-defined]
        pooled_clients.append(retiring)
    _retiring_clients.clear()
    for pooled in pooled_clients:
        with contextlib.suppress(Exception):
            await pooled.client.aclose()
    if pooled_clients:
        logger.debug(f"已关闭 {len(pooled_clients)} 个复用 HTTP 客户端")


def _resolve_tenant_egress(tenant_egress: Optional[bool], base_url: Any, api_key: Any, proxy_url: Any) -> bool:
    """调用方没说明归属时按连接参数判：第三方插件拿租户选的组直接调这些接口时，不能默认按平台方连"""
    if tenant_egress is not None:
        return bool(tenant_egress)
    from nekro_agent.tools.tenant_egress import is_tenant_owned_endpoint

    return is_tenant_owned_endpoint(base_url, api_key, proxy_url)


def _same_text(left: Any, right: Any) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def _resolve_is_gemini(api_format: Optional[str], base_url: Any, model: Any, api_key: Any) -> bool:
    """这次聊天请求走不走 Gemini 原生

    调用方传了 `api_format` 就照它：只有 `gemini`（不分大小写、去空白）走原生，其他任何值都走 OpenAI 兼容。

    没传时（第三方插件直接调本函数，不知道有这个参数）按连接参数找运行时视图里的模型组：
    地址、模型名、Key 三项都对得上、并且声明了 Gemini 原生的聊天组才走原生，其余一律 OpenAI 兼容。
    这样插件拿到一个 Gemini 原生的组照常能用；没切格式的组，请求与改造前逐字节相同。
    """
    if api_format is not None:
        return str(api_format).strip().lower() == "gemini"
    try:
        groups = list(config.MODEL_GROUPS.values())
    except Exception:  # noqa: BLE001 - 判不出来就按默认格式
        return False
    return any(
        getattr(group, "API_FORMAT", "openai") == "gemini"
        and getattr(group, "MODEL_TYPE", "chat") == "chat"
        and _same_text(group.BASE_URL, base_url)
        and _same_text(group.CHAT_MODEL, model)
        and _same_text(group.API_KEY, api_key)
        for group in groups
    )


def _extract_model_ids(payload: Dict[str, Any]) -> List[str]:
    """从 OpenAI 兼容模型列表响应中提取模型 ID。"""

    def pick(items: Any) -> List[str]:
        if not isinstance(items, list):
            return []
        result: List[str] = []
        for item in items:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                result.append(item["id"])
        return result

    return sorted(set([*pick(payload.get("data")), *pick(payload.get("models"))]))


async def list_openai_models(
    *,
    base_url: str,
    api_key: Optional[str] = None,
    proxy_url: Optional[str] = None,
    tenant_egress: bool,
) -> List[str]:
    """通过服务端真实网络出口拉取 OpenAI 兼容模型列表。

    `tenant_egress`：地址是不是租户给的（必填：这个接口直接拿请求体里的地址去连，调用方按当前用户判）。

    用一次性的客户端、用完就关，不进 `_get_http_client` 的缓存：地址直接来自请求体，按地址缓存的话，
    换着填几十个不同的主机名就能把平台方正在用的客户端挤出缓存。
    """

    normalized_base_url = base_url.strip().rstrip("/")
    models_url = f"{normalized_base_url}/models"
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    async with _create_http_client(
        proxy_url=proxy_url,
        read_timeout=60,
        write_timeout=60,
        connect_timeout=10,
        pool_timeout=10,
        tenant_egress=tenant_egress,
    ) as client:
        response = await client.get(models_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("模型列表响应格式无效")
    return _extract_model_ids(payload)


async def gen_openai_chat_response(
    model: str,
    messages: Any,
    temperature: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_words: Optional[List[str]] = None,
    max_tokens: Optional[int] = None,
    extra_body: Optional[Union[Dict[str, Any], str]] = None,
    top_k: Optional[int] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    proxy_url: Optional[str] = None,
    stream_mode: bool = False,
    max_wait_time: Optional[int] = None,
    first_token_timeout: Optional[int] = None,
    thought_chain_field_name: str = "reasoning_content",
    chunk_callback: Optional[_AsyncFunc] = None,
    log_path: Optional[Union[str, Path]] = None,
    error_log_path: Optional[Union[str, Path]] = None,
    log_style: Literal["json", "text", "auto"] = "auto",
    tenant_egress: Optional[bool] = None,
    pooled_client: bool = True,
    api_format: Optional[str] = None,
) -> OpenAIResponse:
    """生成聊天回复内容

    `tenant_egress`：模型组归租户时传 True（`tenant_egress.is_tenant_owned_group(组名)`），连接按租户出站建。
    本仓库里的调用处都必须显式传（`tests/test_tenant_egress_callsites.py` 钉着）；不传（第三方插件）时按
    地址、密钥、代理判（`is_tenant_owned_endpoint`），判不出来按租户。
    `pooled_client=False`：用一次性的客户端、用完就关，不进按地址缓存的连接池（测试连接用：地址可能直接来自
    请求体，按地址缓存的话换着填主机名就能把平台方的客户端挤出缓存）。
    """

    _start_time: float = time.time()

    # 接口格式：写了 gemini 才走原生分支；openai / "OpenAI " / 任何不认识的值都逐字走原来的
    # OpenAI 代码，请求参数一个都不变。没传时按连接参数找声明了 Gemini 原生的组（见 `_resolve_is_gemini`）。
    is_gemini = _resolve_is_gemini(api_format, base_url, model, api_key)

    # messages 处理
    messages = [msg.to_dict() if isinstance(msg, OpenAIChatMessage) else msg for msg in messages]

    # Parse extra_body if it is a string
    if isinstance(extra_body, str):
        extra_body = parse_extra_body(extra_body, source_hint=f"Model: {model}")

    gen_kwargs = {
        "temperature": temperature,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stop": stop_words,
        "extra_body": dict(extra_body) if extra_body else None,
    }

    if top_k is not None:
        if gen_kwargs["extra_body"] is None:
            gen_kwargs["extra_body"] = {}
        gen_kwargs["extra_body"]["top_k"] = top_k

    # 去掉所有值为None的键
    gen_kwargs = {key: value for key, value in gen_kwargs.items() if value is not None}

    # messages 处理
    messages = [msg.to_dict() if isinstance(msg, OpenAIChatMessage) else msg for msg in messages]

    output: str = ""
    thought_chain: str = ""
    token_consumption: int = 0
    token_input: int = 0
    token_input_cached: int = 0
    token_output: int = 0
    first_token_time: Optional[float] = None
    # 上游给出的收尾原因（已归一化）。流式与非流式各自往这里写一次，
    # 之后两条分支用**同一套**判据翻译成异常——判据只有一份，才不会又出现「第二处实现没跟上」
    finish_reason: str = ""

    def _extract_valid_delta(chunk: ChatCompletionChunk) -> Optional[Any]:
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            if chunk.usage:
                logger.debug(f"OpenAI流式生成: 收到仅包含 usage 的空 choices 块 {chunk}")
            else:
                logger.warning(f"OpenAI流式生成: 空 choices，跳过块 {chunk}")
            return None
        return delta

    async def _apply_stream_chunk(chunk: ChatCompletionChunk) -> bool:
        nonlocal output, thought_chain, token_consumption, token_input, token_input_cached, token_output, first_token_time
        nonlocal finish_reason

        # finish_reason 只出现在收尾块上，而收尾块的 delta 常常是空的（拒答时更是必然如此）。
        # 所以必须在 `_extract_valid_delta` 那条空 delta 早退**之前**记下来，
        # 否则整条流式路径永远看不到 `prohibited_content`。
        if chunk.choices:
            chunk_finish_reason = _choice_finish_reason(chunk.choices[0])
            if chunk_finish_reason:
                finish_reason = chunk_finish_reason

        # usage 也必须在空 delta 早退**之前**取。`stream_options={"include_usage": True}`
        # 下，OpenAI 兼容上游把 usage 放在最后一个 `choices: []` 的块上——而那正是
        # `_extract_valid_delta` 判成"空 delta、跳过"的那一类。改造前这一段排在早退之后，
        # 于是**整条流式路径的 token 数恒为 0**：`OpenAIResponse.token_*` 全是 0，
        # `prompts_log/*.json` 的 metrics 全是 0，`routers/dashboard.py` 里 token 0 命中。
        # 那个 `logger.debug("收到仅包含 usage 的空 choices 块")` 就是它每次都被扔掉的现场。
        if chunk.usage:
            if chunk.usage.total_tokens is not None:
                token_consumption = chunk.usage.total_tokens
            if chunk.usage.prompt_tokens is not None:
                token_input = chunk.usage.prompt_tokens
            chunk_cached = _cached_input_tokens(chunk.usage)
            if chunk_cached is not None:
                token_input_cached = chunk_cached
            if chunk.usage.completion_tokens is not None:
                token_output = chunk.usage.completion_tokens

        delta = _extract_valid_delta(chunk)
        if delta is None:
            return False

        if not first_token_time:
            first_token_time = time.time()

        chunk_text: Optional[str] = delta.content
        if chunk_text:
            output += chunk_text
        current_thought_chain = getattr(delta, thought_chain_field_name, "") or ""
        thought_chain += current_thought_chain

        if chunk_callback and await chunk_callback(
            OpenAIStreamChunk(
                chunk_text=chunk_text or "",
                thought_chain=current_thought_chain or "",
                token_consumption=token_consumption,
                token_input=token_input,
                token_output=token_output,
            ),
        ):
            return True
        return False

    # 首 Token 时限从**发出请求**算起，等响应头与等首个数据块共用这一个截止时刻。
    # `create(stream=True)` 要等上游回了响应头才返回，上游排队、或中转站等它的上游回头之后
    # 才回头时，这一段可以很长；只给「等首块」计时的话，这一段只受读超时约束，
    # 首 Token 时限形同虚设。两段各给一整份时限也不对，最坏要等两倍。
    first_token_deadline: Optional[float] = (
        asyncio.get_running_loop().time() + first_token_timeout
        if stream_mode and first_token_timeout and first_token_timeout > 0
        else None
    )

    async def _before_first_token_deadline(awaitable: Awaitable[_T]) -> _T:
        """在首 Token 截止时刻之前等完 `awaitable`；没设时限（或非流式）时就是普通的 await"""
        timer = asyncio.timeout_at(first_token_deadline)
        try:
            async with timer:
                return await awaitable
        except TimeoutError as exc:
            # 只翻译这道时限自己到点的那一种，里面冒出来的别的 TimeoutError 原样抛
            if not timer.expired():
                raise
            raise TimeoutError(f"等待流式响应首个数据块超时，已超过 {first_token_timeout} 秒") from exc

    async def _get_first_stream_chunk(stream: AsyncStream[ChatCompletionChunk]) -> ChatCompletionChunk:
        stream_iter = stream.__aiter__()

        async def wait_first_chunk() -> ChatCompletionChunk:
            try:
                return await stream_iter.__anext__()
            except StopAsyncIteration as exc:
                # 「上游 2xx 但正文为空」在流式路径上有**两个**判据点：这里（一个数据块都没有）
                # 与下面那条（收到了块但最终没有正文）。两条必须归同一类，否则同一件事
                # 会按上游是「直接关流」还是「发空块后关流」给出两种分类与两种用户回执。
                raise EmptyLLMResponseError("流式响应在返回首个数据块前已结束，API 供应商返回了空流") from exc

        return await _before_first_token_deadline(wait_first_chunk())

    # http_client 由本模块的连接池持有，因此既不能用 async with 托管它，
    # 也不能用 async with 托管 AsyncOpenAI —— 后者退出时会连带关闭传入的 http_client。
    # 一次性的客户端（`pooled_client=False`）在下面的 finally 里关
    one_off_client: Optional[httpx.AsyncClient] = None
    try:
        wait_timeout = max_wait_time or 3600
        if pooled_client:
            http_client = _get_http_client(
                base_url=base_url,
                proxy_url=proxy_url,
                read_timeout=wait_timeout,
                write_timeout=wait_timeout,
                tenant_egress=_resolve_tenant_egress(tenant_egress, base_url, api_key, proxy_url),
            )
        else:
            http_client = one_off_client = _create_http_client(
                proxy_url=proxy_url,
                read_timeout=wait_timeout,
                write_timeout=wait_timeout,
                tenant_egress=_resolve_tenant_egress(tenant_egress, base_url, api_key, proxy_url),
            )
        if is_gemini:
            gemini_url = gemini_native.gemini_model_endpoint(base_url, model, stream=stream_mode)
            gemini_request_headers = gemini_native.gemini_headers(api_key)
            gemini_body = gemini_native.gemini_request_body(
                messages,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                stop_words=stop_words,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
                extra_body=extra_body,
            )
            block_reason = ""
            if stream_mode:
                gemini_usage_value = gemini_native.GeminiUsageAccumulator()
                # 首 Token 时限从**发出请求**算起：进流与拿首个事件共用同一个截止时刻
                gemini_stream_ctx = http_client.stream(
                    "POST",
                    gemini_url,
                    json=gemini_body,
                    headers=gemini_request_headers,
                )
                gemini_response = await _before_first_token_deadline(gemini_stream_ctx.__aenter__())
                try:
                    if gemini_response.is_error:
                        await gemini_native.raise_gemini_http_error(gemini_response)
                    gemini_events = gemini_native.iter_gemini_sse_events(gemini_response).__aiter__()

                    async def _wait_first_event() -> Dict[str, Any]:
                        try:
                            return await gemini_events.__anext__()
                        except StopAsyncIteration as exc:
                            # 与 OpenAI 分支同一个判据点：一个事件都没有也算「2xx 但空流」
                            raise EmptyLLMResponseError(
                                "流式响应在返回首个数据块前已结束，API 供应商返回了空流",
                            ) from exc

                    pending_event: Optional[Dict[str, Any]] = await _before_first_token_deadline(_wait_first_event())
                    while pending_event is not None:
                        event = pending_event
                        pending_event = None
                        gemini_native.raise_gemini_event_error(event)
                        gemini_usage_value.update(event)
                        token_consumption = gemini_usage_value.token_total
                        token_input = gemini_usage_value.token_input
                        token_input_cached = gemini_usage_value.token_input_cached
                        token_output = gemini_usage_value.token_output
                        event_finish_reason = gemini_native.gemini_finish_reason(event)
                        if event_finish_reason:
                            finish_reason = event_finish_reason
                        event_block_reason = gemini_native.gemini_block_reason(event)
                        if event_block_reason:
                            block_reason = event_block_reason
                        event_text, event_thought = gemini_native.gemini_text_and_thought(event)
                        if (event_text or event_thought) and not first_token_time:
                            first_token_time = time.time()
                        if event_thought:
                            thought_chain += event_thought
                        if event_text:
                            output += event_text
                            if chunk_callback and await chunk_callback(
                                OpenAIStreamChunk(
                                    chunk_text=event_text,
                                    thought_chain=event_thought,
                                    token_consumption=token_consumption,
                                    token_input=token_input,
                                    token_output=token_output,
                                ),
                            ):
                                break
                        try:
                            pending_event = await gemini_events.__anext__()
                        except StopAsyncIteration:
                            pending_event = None
                finally:
                    await gemini_stream_ctx.__aexit__(None, None, None)

                # 闸门顺序与 OpenAI 分支完全一致：先判被拦，再救回纯思考，最后判空
                if block_reason or finish_reason in CONTENT_BLOCKED_FINISH_REASONS:
                    finish_reason = block_reason or finish_reason
                    raise LLMContentBlockedError(finish_reason, stream_mode=True)  # noqa: TRY301
                output = _salvage_reasoning_only_output(
                    output,
                    thought_chain,
                    model=model,
                    field_name=thought_chain_field_name,
                )
                if not output:
                    if finish_reason == MALFORMED_FUNCTION_CALL_FINISH_REASON:
                        raise LLMMalformedToolCallError(stream_mode=True)  # noqa: TRY301
                    raise EmptyLLMResponseError("流式响应未返回任何有效内容（正文与思考通道均为空）")  # noqa: TRY301
            else:
                gemini_response = await http_client.post(
                    gemini_url,
                    json=gemini_body,
                    headers=gemini_request_headers,
                )
                if gemini_response.is_error:
                    await gemini_native.raise_gemini_http_error(gemini_response)
                gemini_payload = gemini_response.json()
                if not isinstance(gemini_payload, dict):
                    raise ValueError("Gemini 原生响应格式无效")  # noqa: TRY301

                # usage 同样在任何一道闸门之前读出来：被拦与判空的那两类也已经计过费
                gemini_usage_value = gemini_native.gemini_usage(gemini_payload)
                token_consumption = gemini_usage_value.token_total or 0
                token_input = gemini_usage_value.token_input or 0
                token_input_cached = gemini_usage_value.token_input_cached or 0
                token_output = gemini_usage_value.token_output or 0

                block_reason = gemini_native.gemini_block_reason(gemini_payload)
                finish_reason = gemini_native.gemini_finish_reason(gemini_payload)
                if block_reason or finish_reason in CONTENT_BLOCKED_FINISH_REASONS:
                    finish_reason = block_reason or finish_reason
                    raise LLMContentBlockedError(finish_reason, stream_mode=False)  # noqa: TRY301

                gemini_text, gemini_thought = gemini_native.gemini_text_and_thought(gemini_payload)
                thought_chain = gemini_thought
                output = _salvage_reasoning_only_output(
                    gemini_text,
                    thought_chain,
                    model=model,
                    field_name=thought_chain_field_name,
                )
                if not output:
                    if finish_reason == MALFORMED_FUNCTION_CALL_FINISH_REASON:
                        raise LLMMalformedToolCallError(stream_mode=False)  # noqa: TRY301
                    raise EmptyLLMResponseError(
                        f"非流式响应内容为空，供应商未返回可用消息。原始响应: {gemini_payload}",
                    )  # noqa: TRY301
        else:
            client = AsyncOpenAI(
                api_key=api_key.strip() if api_key else None,
                base_url=base_url or _OPENAI_BASE_URL,
                http_client=http_client,
                max_retries=0,
            )

            if stream_mode:
                res_stream: AsyncStream[ChatCompletionChunk] = await _before_first_token_deadline(
                    client.chat.completions.create(
                        model=model,
                        messages=messages,
                        **gen_kwargs,
                        stream=True,
                        stream_options={"include_usage": True},
                    ),
                )
                # 连接池是共享的，提前 break 时必须显式关闭响应把连接还回池中，
                # 否则该连接会一直被占用直到超时
                async with res_stream:
                    first_chunk = await _get_first_stream_chunk(res_stream)
                    if first_chunk and not first_token_time:
                        first_token_time = time.time()
                    if first_chunk and await _apply_stream_chunk(first_chunk):
                        pass
                    else:
                        async for chunk in res_stream:
                            if await _apply_stream_chunk(chunk):
                                break
                # 判空**之前**先看收尾原因：拦截是上游对整条回复的判决，就算它先吐了半截正文
                # （OpenAI 的 content_filter 可能在流中途触发），那半截也必然是被截断的，
                # 送进 `parse_chat_response` 只会得到半个代码块——正是本轮在修的那类地雷。
                # 宁可归成一次可辨识的失败，也不把截断的残句当成模型的回答。
                if finish_reason in CONTENT_BLOCKED_FINISH_REASONS:
                    raise LLMContentBlockedError(finish_reason, stream_mode=True)  # noqa: TRY301
                output = _salvage_reasoning_only_output(
                    output,
                    thought_chain,
                    model=model,
                    field_name=thought_chain_field_name,
                )
                # 判据从「没收到过任何数据块」改成「最终没有任何可用正文」：
                # reasoning-only 的流会在读 delta.content **之前**就把 first_token_time 置上，
                # 旧判据因此恒不成立，`response_content=""` 会一路走到沙盒执行一段空脚本、
                # 以 NORMAL 退出——Bot 一言不发而日志里连一条 ERROR 都没有。
                if not output:
                    # 与拦截相反，这一支只在正文确实为空时才成立：本项目不消费原生工具调用，
                    # 模型顺带写坏一个 function call 但正文可用时，那份正文照常拿去解析
                    if finish_reason == MALFORMED_FUNCTION_CALL_FINISH_REASON:
                        raise LLMMalformedToolCallError(stream_mode=True)  # noqa: TRY301
                    raise EmptyLLMResponseError("流式响应未返回任何有效内容（正文与思考通道均为空）")  # noqa: TRY301
            else:
                res: ChatCompletion = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **gen_kwargs,
                    stream=False,
                )
                # usage 必须在**任何一道闸门之前**读出来。改造前它在函数最末尾，于是
                # 「被内容策略拦下」「正文为空」这两类响应的 token 数一个都记不到 ——
                # 而那一类恰恰是**已经计过费**的（见 `EmptyLLMResponseError` 的 docstring：
                # 实测有中转站在 completion_tokens=0、reasoning_tokens=1116 时照样计费）。
                # 对成功响应而言这一挪逐字等价：读的是同一个对象的同三个字段。
                token_consumption = res.usage.total_tokens if res.usage else 0
                token_input = res.usage.prompt_tokens if res.usage else 0
                token_input_cached = _cached_input_tokens(res.usage) or 0
                token_output = res.usage.completion_tokens if res.usage else 0
                if not res.choices or len(res.choices) == 0:
                    raise EmptyLLMResponseError(f"非流式响应没有任何 choices，供应商未返回可用消息。原始响应: {res}")  # noqa: TRY301

                # 与流式分支同一套判据（`CONTENT_BLOCKED_FINISH_REASONS` / 判空之前先判拦截）。
                # 两条分支各判一次是刻意的：现场同一个模型组既可能开流式也可能不开，
                # 只在一条分支上判等于一半的请求照旧被归成「一个字都没返回」
                finish_reason = _choice_finish_reason(res.choices[0])
                if finish_reason in CONTENT_BLOCKED_FINISH_REASONS:
                    raise LLMContentBlockedError(finish_reason, stream_mode=False)  # noqa: TRY301

                thought_chain = getattr(res.choices[0].message, thought_chain_field_name, "") or ""
                output = _salvage_reasoning_only_output(
                    res.choices[0].message.content or "",
                    thought_chain,
                    model=model,
                    field_name=thought_chain_field_name,
                )
                if not output:
                    if finish_reason == MALFORMED_FUNCTION_CALL_FINISH_REASON:
                        raise LLMMalformedToolCallError(stream_mode=False)  # noqa: TRY301
                    raise EmptyLLMResponseError(f"非流式响应内容为空，供应商未返回可用消息。原始响应: {res}")  # noqa: TRY301

    except asyncio.CancelledError:
        # **`CancelledError` 不是 `Exception`**（3.8 起挂在 `BaseException` 下），下面那条
        # `except Exception` 一个字都接不到它。而取消是这条函数**真实存在**的收尾方式：
        # `templates/history.py` 用 `asyncio.wait_for(planning_call, timeout=…)` 裹住本函数，
        # 超时那一下就是往里抛 `CancelledError`。少了本分支，那条链路超时收尾时账本上
        # **一行都没有**——而模型文档要求的正是"失败的那几次也记，'今天失败了 400 次'本身就是运营信号"。
        # 收尾形态取 `ABORTED` 而不是 `FAILED`：与 `gen_openai_chat_stream` 里"消费方提前
        # 丢弃生成器"同一档（既不成功也不失败，token 却可能已经烧了），失败率指标因此不失真。
        record_llm_usage(
            model_name=model,
            stream_mode=stream_mode,
            token_input=token_input,
            token_input_cached=token_input_cached,
            token_output=token_output,
            token_total=token_consumption,
            status=LLMUsageStatus.ABORTED,
            error_type="CancelledError",
            finish_reason=finish_reason,
            generation_time_ms=int((time.time() - _start_time) * 1000),
        )
        raise
    except Exception as e:
        # 记账在**最前面**：下面几步（脱敏、渲染堆栈、落盘错误日志）任何一步再出问题，
        # 这一次真实的花销都已经进了账本。失败的请求同样烧钱——流式在被拦/判空之前
        # 往往已经收到过带 usage 的块，非流式的 usage 也在上面的闸门之前就读出来了。
        record_llm_usage(
            model_name=model,
            stream_mode=stream_mode,
            token_input=token_input,
            token_input_cached=token_input_cached,
            token_output=token_output,
            token_total=token_consumption,
            status=LLMUsageStatus.FAILED,
            error_type=type(e).__name__,
            finish_reason=finish_reason,
            generation_time_ms=int((time.time() - _start_time) * 1000),
        )
        # 上游报错体由上游决定内容，供应商回显凭据是常见形态（`Incorrect API key provided: sk-…`、
        # `?key=` 拼进 URL 的 4xx、反代把整条 Authorization 头抄回来的 5xx）。
        # 本次请求实际用的 `api_key` / `proxy_url` 可能来自会话或实例覆盖层，
        # 全局配置树里未必有，所以要显式并进凭据集合。
        secret_values = collect_llm_secret_values(api_key, proxy_url)
        # 这里**不能**用 `logger.exception`：loguru 的 `diagnose` 默认开着，会把每一行源码里
        # 出现的名字连同取值一起渲染出来——本函数抛错的那一行正好带着 `messages`（整份 prompt，
        # 含图片 base64）与 `gen_kwargs`，而那份诊断文本不经过 f-string，脱不掉。
        # 实测一次带 `sk-…` 的 401，哨兵在 sink 里出现 4 次，f-string 只占其中 1 次。
        # 改用 stdlib 的 traceback 渲染（不含局部变量）+ 整体脱敏，堆栈信息一行不少。
        logger.error(
            f"OpenAI请求失败[{type(e).__name__}]: {redact_upstream_text(str(e), secret_values)}\n"
            f"{format_redacted_traceback(e, secret_values)}",
        )
        response = OpenAIErrResponse.create_from_exception(
            e,
            use_model=model,
            stream_mode=stream_mode,
            messages=messages,
            log_path=error_log_path,
            secret_values=secret_values,
        )
        if error_log_path:
            await response.save_log(
                log_path=error_log_path,
                log_style=log_style,
                messages=messages,
                message_cnt=len(messages) + 1,
            )
        raise
    finally:
        if one_off_client is not None:
            await _aclose_quietly(one_off_client)

    # 时间统计
    _end_time: float = time.time()
    _generation_time_ms: int = int((_end_time - _start_time) * 1000)
    # `_generation_time_ms` 是取整到毫秒的：不到 1ms 的响应（本地/同机上游、命中缓存的重放）
    # 会让它恰好为 0，于是这一行抛 ZeroDivisionError。它在 `except Exception` **之后**，
    # 所以既不会被翻译成可辨识的失败、也不会落盘错误日志——一次成功的请求会以一条
    # 与 LLM 毫无关系的异常收场。速率统计是观测量，算不出来就当 0，不能反过来打掉整轮对话。
    _speed_tokens_per_second: float = (token_output * 1000 / _generation_time_ms) if _generation_time_ms > 0 else 0.0
    _first_token_cost_ms: Optional[int] = None
    if first_token_time:
        _first_token_cost_ms = int((first_token_time - _start_time) * 1000)

    response = OpenAIResponse(
        response_content=output,
        thought_chain=thought_chain,
        messages=messages,
        message_cnt=len(messages) + 1,
        token_consumption=token_consumption,
        token_input=token_input,
        token_output=token_output,
        use_model=model,
        speed_tokens_per_second=_speed_tokens_per_second,
        first_token_cost_ms=_first_token_cost_ms or 0,
        generation_time_ms=_generation_time_ms,
        stream_mode=stream_mode,
        log_path=log_path,
    )

    # 成功侧记账。流式与非流式在这里合流，是因为两条分支写的是**同一组**
    # `token_input/token_output/token_consumption` 局部变量（流式在 `_apply_stream_chunk`
    # 里累加，非流式在闸门之前一次读出）。分成两处写等于同一个判据的第二处实现。
    record_llm_usage(
        model_name=model,
        stream_mode=stream_mode,
        token_input=token_input,
        token_input_cached=token_input_cached,
        token_output=token_output,
        token_total=token_consumption,
        status=LLMUsageStatus.SUCCESS,
        finish_reason=finish_reason,
        generation_time_ms=_generation_time_ms,
    )

    if log_path:
        await response.save_log(
            log_path=log_path,
            log_style=log_style,
            messages=messages,
            message_cnt=len(messages) + 1,
            temperature=temperature,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            top_p=top_p,
            max_tokens=max_tokens,
            stop_words=stop_words,
        )

    return response


async def gen_openai_embeddings(
    model: str,
    input: Union[str, List[Dict[str, Any]]],  # noqa: A002
    api_key: str,
    base_url: str,
    dimensions: Optional[int] = None,
    proxy_url: Optional[str] = None,
    endpoint: str = "/embeddings",
    timeout: int = 10,
    tenant_egress: Optional[bool] = None,
    pooled_client: bool = True,
) -> List[float]:
    """生成文本的向量表示

    Args:
        model: 模型名称
        input: 输入文本或文本列表
        api_key: API 密钥
        base_url: API 基础地址
        dimensions: 向量维度（可选，不传则由模型决定默认维度）
        proxy_url: 代理地址
        endpoint: API 端点
        timeout: 请求超时时间（秒），默认 10 秒
        pooled_client: False 时用一次性的客户端、用完就关（见 `gen_openai_chat_response`）

    Returns:
        嵌入向量

    ## 这里也记账（第四个 usage 读取处）

    交接报告把这条链路记成"账本上唯一一条结构性缺口"，理由是"上游回的是纯向量、
    要记得先改返回形态"——**那条理由是错的**。上游回的是一整个 JSON，
    `{"data": [...], "usage": {"prompt_tokens": N, "total_tokens": N}}`，本函数本来就
    `res.json()` 了一次；记账是**副作用**不是返回值，多记一行不需要动签名。

    而它的量恰恰是全仓最大的一档：一次增强记忆检索会按 query 条数打出 N 次向量化，
    知识库入库与表情包各还有一条。不记的话，账面上"这个客户烧了多少"少掉的正是
    **条数最多**的那一类。补全（`token_output`）恒为 0 是嵌入接口的形态，不是漏读。

    失败也记，与 chat 两条分支同一条理由：`raise_for_status()` 之前上游已经算过钱的
    那些 4xx/5xx 拿不到 usage（两列为 0），但"这个实例今天向量化失败了 400 次"本身
    就是运营信号。`CancelledError` 单独一支，理由见 `gen_openai_chat_response`
    的同名分支（它不是 `Exception`，`except Exception` 一个字都接不到）。
    """
    _start_time: float = time.time()
    token_input: int = 0
    token_total: int = 0

    def _record(status: LLMUsageStatus, error_type: str = "") -> None:
        record_llm_usage(
            model_name=model,
            stream_mode=False,
            token_input=token_input,
            # 嵌入接口没有补全通道，这一列恒 0 是形态而不是漏读
            token_output=0,
            token_total=token_total,
            status=status,
            error_type=error_type,
            generation_time_ms=int((time.time() - _start_time) * 1000),
        )

    egress = _resolve_tenant_egress(tenant_egress, base_url, api_key, proxy_url)
    one_off_client: Optional[httpx.AsyncClient] = None
    if pooled_client:
        client = _get_http_client(
            base_url=base_url,
            proxy_url=proxy_url,
            read_timeout=timeout,
            write_timeout=timeout,
            tenant_egress=egress,
        )
    else:
        client = one_off_client = _create_http_client(
            proxy_url=proxy_url,
            read_timeout=timeout,
            write_timeout=timeout,
            tenant_egress=egress,
        )
    payload: dict[str, object] = {"model": model, "input": input}
    if dimensions is not None:
        payload["dimensions"] = dimensions

    data = json.dumps(payload, ensure_ascii=False)

    try:
        res = await client.post(
            f"{base_url}{endpoint}",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {api_key.strip()}",
            },
            content=data.encode("utf-8"),
        )
        res.raise_for_status()
        body = res.json()
        # usage 在**取向量之前**读：响应体不合形态（中转站包了一层、data 为空）时
        # 这一跳同样已经计过费，而取向量那一行会抛 KeyError/IndexError。
        # 与非流式 chat 分支"usage 必须在任何一道闸门之前读出来"是同一条判据。
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            token_input = int(usage.get("prompt_tokens") or 0)
            token_total = int(usage.get("total_tokens") or token_input)
        vector = body["data"][0]["embedding"]
    except asyncio.CancelledError:
        _record(LLMUsageStatus.ABORTED, "CancelledError")
        raise
    except Exception as e:
        _record(LLMUsageStatus.FAILED, type(e).__name__)
        # 按模型组记成败，长期失败时告警一次（判据与收件人见 `services/embedding_health.py`）
        embedding_health.note_failure(e)
        raise
    finally:
        if one_off_client is not None:
            await _aclose_quietly(one_off_client)
    _record(LLMUsageStatus.SUCCESS)
    embedding_health.note_success()
    return vector


async def gen_openai_chat_stream(
    model: str,
    messages: List[Union[OpenAIChatMessage, Dict[str, Any]]],
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    proxy_url: Optional[str] = None,
    temperature: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_words: Optional[List[str]] = None,
    max_tokens: Optional[int] = None,
    extra_body: Optional[Union[Dict[str, Any], str]] = None,
    top_k: Optional[int] = None,
    tenant_egress: Optional[bool] = None,
    api_format: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """简化的OpenAI流式生成器，直接产生文本片段

    这是一个简化版的流式生成器，直接返回生成的文本片段，而不是复杂的OpenAIResponse对象。
    适用于需要简单流式输出的场景，如网页UI中的实时代码生成。

    ## 它是同一套判据的**第三个**入口

    `gen_openai_chat_response` 的流式与非流式两条分支各自都有「先判 `finish_reason` 再判空」
    这道闸门，而本函数此前**一条判据都没有**：既不判上游拒答、也不抛 `EmptyLLMResponseError`，
    连 `include_usage` 都没要。表现是 WebUI 的实时代码生成在被内容策略拦下、或者遇上
    原生思考模型（整段答案走 `reasoning_content`）时**一个字都不产出、也不报错**，
    页面停在"生成中"直到用户自己放弃。

    现在与那两条分支共用同一套判据（同一个常量集合、同一个救回函数、同一组异常类）：

    1. `finish_reason ∈ CONTENT_BLOCKED_FINISH_REASONS` → `LLMContentBlockedError`；
    2. 正文全空但思考通道有内容 → `_salvage_reasoning_only_output` 取回来，
       在流末尾补吐一次（流式没法回溯，只能补在最后，但内容与非流式逐字相同）；
    3. 仍然没有正文 → `LLMMalformedToolCallError` / `EmptyLLMResponseError`。

    ## 用量计量

    这一路同样烧平台方的 Key，因此 `stream_options={"include_usage": True}` 是**必需**的，
    否则上游根本不会在流末尾带 usage 块，本入口的账永远是 0。
    收尾形态分三档：正常读完（`SUCCESS`）、抛异常（`FAILED`）、
    以及消费方提前丢弃生成器（`ABORTED` —— WebUI 关掉页面就是这一支，token 照样烧了）。

    Args:
        model: 模型名称
        messages: 消息列表，可以是OpenAIChatMessage对象或字典
        base_url: OpenAI基础URL
        api_key: OpenAI API密钥
        temperature: 温度参数，控制随机性
        frequency_penalty: 频率惩罚
        presence_penalty: 存在惩罚
        top_p: Top-p采样
        stop_words: 停止词列表
        max_tokens: 最大生成token数

    Yields:
        生成的文本片段
    """
    logger.info(f"启动简化的流式生成，使用模型: {model}")

    # Parse extra_body if it is a string
    if isinstance(extra_body, str):
        extra_body = parse_extra_body(extra_body, source_hint=f"Model: {model}")

    # 创建参数字典，移除None值
    # 创建参数字典，移除None值
    gen_kwargs = {
        "temperature": temperature,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stop": stop_words,
        "extra_body": dict(extra_body) if extra_body else None,
    }

    if top_k is not None:
        if gen_kwargs["extra_body"] is None:
            gen_kwargs["extra_body"] = {}
        gen_kwargs["extra_body"]["top_k"] = top_k

    gen_kwargs = {k: v for k, v in gen_kwargs.items() if v is not None}

    # 处理消息格式
    formatted_messages = []
    for msg in messages:
        if isinstance(msg, OpenAIChatMessage):
            formatted_messages.append(msg.to_dict())
        else:
            formatted_messages.append(msg)

    _start_time: float = time.time()
    output: str = ""
    thought_chain: str = ""
    token_consumption: int = 0
    token_input: int = 0
    token_input_cached: int = 0
    token_output: int = 0
    finish_reason: str = ""
    # 收尾形态在 `finally` 里定档。默认是 `ABORTED`：消费方提前丢弃生成器时
    # `finally` 是唯一还会跑到的地方，而那一支既不成功也不失败，token 却已经烧了。
    status: LLMUsageStatus = LLMUsageStatus.ABORTED
    error_type: str = ""
    # 归属在**入口**就快照下来，`_record()` 里不再去读当时的 contextvar。
    #
    # 异步生成器没有自己的上下文（PEP 568 至今未落地），它的每一步都跑在**当次驱动它的那个
    # 任务**里。而 `ABORTED` 这一档（消费方提前丢弃生成器）的收尾恰恰不是消费方跑的：
    # asyncio 的 asyncgen finalizer 用 `loop.create_task(agen.aclose())` 关它，那个任务
    # 的上下文是新拷贝的，`current_usage_context()` 在里面读到的是**默认值**。
    # 也就是说改造前这条链路唯一真正需要 `ABORTED` 的场景（WebUI 关掉插件生成页面），
    # 记出来的账恰好整条落进 `unattributed`——而现有用例在同一个任务里 `aclose()`，
    # 分辨不出这两种收尾。入口快照对正常路径逐字等价（那时读到的就是调用方声明的那份）。
    _usage_context = current_usage_context()

    def _record() -> None:
        """把本次请求记进账本（三条出口共用，见 `finally`）"""
        record_llm_usage(
            model_name=model,
            stream_mode=True,
            token_input=token_input,
            token_input_cached=token_input_cached,
            token_output=token_output,
            token_total=token_consumption,
            status=status,
            error_type=error_type,
            finish_reason=finish_reason,
            generation_time_ms=int((time.time() - _start_time) * 1000),
            context=_usage_context,
        )

    # 创建OpenAI客户端
    try:
        # http_client 来自共享连接池，不能交给 async with 关闭
        http_client = _get_http_client(
            base_url=base_url,
            proxy_url=proxy_url,
            read_timeout=300,
            write_timeout=300,
            tenant_egress=_resolve_tenant_egress(tenant_egress, base_url, api_key, proxy_url),
        )
        is_gemini = _resolve_is_gemini(api_format, base_url, model, api_key)
        if is_gemini:
            gemini_url = gemini_native.gemini_model_endpoint(base_url, model, stream=True)
            gemini_body = gemini_native.gemini_request_body(
                formatted_messages,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                stop_words=stop_words,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
                extra_body=extra_body,
            )
            gemini_usage_value = gemini_native.GeminiUsageAccumulator()
            block_reason = ""
            gemini_stream_ctx = http_client.stream(
                "POST",
                gemini_url,
                json=gemini_body,
                headers=gemini_native.gemini_headers(api_key),
            )
            gemini_response = await gemini_stream_ctx.__aenter__()
            try:
                if gemini_response.is_error:
                    await gemini_native.raise_gemini_http_error(gemini_response)
                async for event in gemini_native.iter_gemini_sse_events(gemini_response):
                    gemini_native.raise_gemini_event_error(event)
                    gemini_usage_value.update(event)
                    token_consumption = gemini_usage_value.token_total
                    token_input = gemini_usage_value.token_input
                    token_input_cached = gemini_usage_value.token_input_cached
                    token_output = gemini_usage_value.token_output
                    event_finish_reason = gemini_native.gemini_finish_reason(event)
                    if event_finish_reason:
                        finish_reason = event_finish_reason
                    event_block_reason = gemini_native.gemini_block_reason(event)
                    if event_block_reason:
                        block_reason = event_block_reason
                    event_text, event_thought = gemini_native.gemini_text_and_thought(event)
                    thought_chain += event_thought
                    if event_text:
                        output += event_text
                        yield event_text
            finally:
                await gemini_stream_ctx.__aexit__(None, None, None)

            # 与 OpenAI 分支同序：先判被拦，再救回纯思考，最后判空
            if block_reason or finish_reason in CONTENT_BLOCKED_FINISH_REASONS:
                finish_reason = block_reason or finish_reason
                raise LLMContentBlockedError(finish_reason, stream_mode=True)  # noqa: TRY301
            salvaged = _salvage_reasoning_only_output(
                output,
                thought_chain,
                model=model,
                field_name="reasoning_content",
            )
            if salvaged and not output:
                output = salvaged
                yield salvaged
            if not output:
                if finish_reason == MALFORMED_FUNCTION_CALL_FINISH_REASON:
                    raise LLMMalformedToolCallError(stream_mode=True)  # noqa: TRY301
                raise EmptyLLMResponseError("流式响应未返回任何有效内容（正文与思考通道均为空）")  # noqa: TRY301
            status = LLMUsageStatus.SUCCESS
        else:
            client = AsyncOpenAI(
                api_key=api_key.strip() if api_key else None,
                base_url=base_url or _OPENAI_BASE_URL,
                http_client=http_client,
                max_retries=0,
            )

            # 创建流式响应。`include_usage` 是本入口能记账的前提：不要它，上游不会在流末尾
            # 补那个只带 usage 的块，这条链路烧的钱就永远算不出来（与另外两条分支同一个要求）
            stream = await client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                stream=True,
                stream_options={"include_usage": True},
                **gen_kwargs,
            )

            # 消费方随时可能提前丢弃本生成器，必须确保响应被关闭以把连接还回共享池
            async with stream:
                # 直接产生文本片段
                async for chunk in stream:
                    # `finish_reason` 只出现在收尾块上，而收尾块的 delta 常常是空的
                    # （拒答时更是必然如此）。所以要在下面那条 `if delta.content` 早退
                    # **之前**记下来，否则这条链路永远看不到 `prohibited_content`——
                    # 与 `_apply_stream_chunk` 里逐字同源的一条教训。
                    if chunk.choices:
                        chunk_finish_reason = _choice_finish_reason(chunk.choices[0])
                        if chunk_finish_reason:
                            finish_reason = chunk_finish_reason
                    # 仅带 usage 的收尾块没有 choices，必须在早退之前取
                    if chunk.usage:
                        if chunk.usage.total_tokens is not None:
                            token_consumption = chunk.usage.total_tokens
                        if chunk.usage.prompt_tokens is not None:
                            token_input = chunk.usage.prompt_tokens
                        chunk_cached = _cached_input_tokens(chunk.usage)
                        if chunk_cached is not None:
                            token_input_cached = chunk_cached
                        if chunk.usage.completion_tokens is not None:
                            token_output = chunk.usage.completion_tokens

                    delta = chunk.choices[0].delta if chunk.choices else None
                    if not delta:
                        continue
                    thought_chain += getattr(delta, "reasoning_content", "") or ""
                    content = delta.content
                    if content:
                        output += content
                        yield content

            # 判空**之前**先看收尾原因：拦截是上游对整条回复的判决，就算它先吐了半截正文，
            # 那半截也必然是被截断的——与另外两条分支同一顺序、同一个常量集合。
            if finish_reason in CONTENT_BLOCKED_FINISH_REASONS:
                raise LLMContentBlockedError(finish_reason, stream_mode=True)  # noqa: TRY301
            # 正文全空但思考通道有内容：原生思考模型把整段答案走了 reasoning 通道。
            # 流式没法回溯，只能在流末尾补吐一次；补出来的字符串与非流式分支逐字相同
            # （同一个 `_salvage_reasoning_only_output`，不是"等价的另一种写法"）。
            salvaged = _salvage_reasoning_only_output(
                output,
                thought_chain,
                model=model,
                field_name="reasoning_content",
            )
            if salvaged and not output:
                output = salvaged
                yield salvaged
            if not output:
                if finish_reason == MALFORMED_FUNCTION_CALL_FINISH_REASON:
                    raise LLMMalformedToolCallError(stream_mode=True)  # noqa: TRY301
                raise EmptyLLMResponseError("流式响应未返回任何有效内容（正文与思考通道均为空）")  # noqa: TRY301
            status = LLMUsageStatus.SUCCESS

    except Exception as e:
        status = LLMUsageStatus.FAILED
        error_type = type(e).__name__
        # 与 `gen_openai_chat_response` 同一条判据：这条链路（WebUI 实时代码生成）
        # 同样直连上游、同样会拿到回显凭据的报错体
        logger.error(
            f"流式生成过程中出错[{type(e).__name__}]: "
            f"{redact_upstream_text(str(e), collect_llm_secret_values(api_key, proxy_url))}",
        )
        raise
    finally:
        # 三条出口（正常读完 / 抛异常 / 被消费方丢弃）共用这一处记账。
        # 放在 `finally` 而不是各写一遍，是因为"被丢弃"那一支根本没有语句可写：
        # 生成器被 close 时只有 `finally` 会跑到。
        _record()
