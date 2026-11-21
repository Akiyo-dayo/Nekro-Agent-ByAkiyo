"""Gemini 原生 REST 适配

模型组上的「接口格式」选 Gemini 原生时，聊天请求不再走 OpenAI 兼容层，而是直接调用
`models/{model}:generateContent` / `:streamGenerateContent?alt=sse`。这一层只负责：

- 把接口地址归一到「版本号为止」的根，拼出模型端点；Key 只放 `x-goog-api-key`，绝不进 URL；
- 把 `to_dict()` 之后的 OpenAI 形态消息转成 `contents` / `systemInstruction`；
- 把采样参数与额外请求体合进 `generationConfig` / `safetySettings`；
- 解析响应与 SSE 事件、把错误翻译成带状态码的 `GeminiAPIError`。

网络客户端一律由 `openai.py` 的 `_get_http_client` / `_create_http_client` 提供：这里不建
`httpx.AsyncClient`，也不读额外请求体里的地址、Key 或请求头。
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, AsyncGenerator, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("gemini_native")

GEMINI_DEFAULT_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# 根路径的最后一段是版本号时原样保留，否则补 `/v1beta`
_VERSION_SEGMENT_RE = re.compile(r"^v\d+(?:alpha|beta)?\d*$")

# `data:<mime>;base64,<data>`：只接受内联图片，远端链接原生接口拿不到
_DATA_URL_RE = re.compile(r"data:(?P<mime>[^;]+);base64,(?P<data>.+)", re.DOTALL)

# 额外请求体里允许直接进原生请求体的顶层键；其余一律丢弃
_ALLOWED_EXTRA_KEYS = frozenset({"generationConfig", "safetySettings", "cachedContent", "response_format", "top_k"})


def gemini_api_root(base_url: Optional[str]) -> str:
    """把模型组上的接口地址归一成 Gemini 原生 API 根

    空值回默认根；末尾斜杠与 `/models` 段去掉；最后一段是版本号（`v1`、`v1beta`、
    `v1alpha`、`v1beta2` 这类）时原样保留，否则补 `/v1beta`。
    地址里带查询串或片段一律拒绝：那正是把 `?key=` 写进地址的形态。
    """
    raw = (base_url or "").strip()
    if not raw:
        return GEMINI_DEFAULT_API_ROOT
    if "?" in raw or "#" in raw:
        raise ValueError("Gemini 原生接口地址不能带查询参数或片段")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Gemini 原生接口地址必须以 http:// 或 https:// 开头")
    if not parts.netloc:
        raise ValueError("Gemini 原生接口地址缺少主机名")

    path = parts.path.rstrip("/")
    if path.endswith("/models"):
        path = path[: -len("/models")].rstrip("/")
    last_segment = path.rsplit("/", 1)[-1]
    if last_segment and _VERSION_SEGMENT_RE.fullmatch(last_segment):
        root_path = path
    else:
        root_path = f"{path}/v1beta" if path else "/v1beta"
    return urlunsplit((parts.scheme, parts.netloc, root_path, "", ""))


def gemini_model_endpoint(base_url: Optional[str], model: str, *, stream: bool) -> str:
    """拼出 `models/{model}:generateContent` 或流式的 `:streamGenerateContent?alt=sse`

    模型名里的 `/`、`?`、`#`、`:` 一律转义，不能借它改路径或加查询串。
    """
    model_id = (model or "").strip()
    if model_id.startswith("models/"):
        model_id = model_id[len("models/") :]
    if not model_id:
        raise ValueError("Gemini 原生请求缺少模型名")

    root = gemini_api_root(base_url)
    quoted = quote(model_id, safe="-._~")
    if stream:
        return f"{root}/models/{quoted}:streamGenerateContent?alt=sse"
    return f"{root}/models/{quoted}:generateContent"


def gemini_headers(api_key: Optional[str]) -> Dict[str, str]:
    """原生请求头：Key 只放 `x-goog-api-key`，空 Key 不加头

    Key 中间夹着换行、空格或控制字符（几个 Key 换行粘进一格是常见的粘法）时直接拒绝，错误里不带取值。
    交给 HTTP 库的话，它抛的异常文本里是转义过的整串 Key：按字面值脱敏对不上（原值里是真换行），
    就会原样进日志和租户看得到的错误摘要。
    """
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key:
        if not _HEADER_SAFE_KEY_RE.fullmatch(key):
            raise ValueError("模型组的 API Key 含有换行、空格或控制字符，放不进请求头；请检查这个组的 Key（一格只填一个）")
        headers["x-goog-api-key"] = key
    return headers


# 请求头取值里 Key 只会是可见 ASCII；别的字符一律当填错
_HEADER_SAFE_KEY_RE = re.compile(r"[\x21-\x7e]+")


def _text_parts(content: Any) -> List[str]:
    """取一条消息里的全部文本片段（原样，不 strip、不合并空白）"""
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    texts: List[str] = []
    for segment in content:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        text = segment.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _image_url(segment: Dict[str, Any]) -> str:
    image = segment.get("image_url")
    if isinstance(image, dict):
        url = image.get("url")
        return url if isinstance(url, str) else ""
    if isinstance(image, str):
        return image
    return ""


def _content_parts(content: Any) -> Tuple[List[Dict[str, Any]], int]:
    """把一条消息的 content 转成 parts；返回 (parts, 跳过的远端图片张数)"""
    if isinstance(content, str):
        return ([{"text": content}] if content else []), 0
    if not isinstance(content, list):
        return [], 0

    parts: List[Dict[str, Any]] = []
    skipped_remote = 0
    for segment in content:
        if not isinstance(segment, dict):
            continue
        segment_type = segment.get("type")
        if segment_type == "text":
            text = segment.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
            continue
        if segment_type != "image_url":
            continue

        url = _image_url(segment)
        if not url:
            logger.debug("Gemini 原生请求跳过一个没有地址的图片片段")
            continue
        if url.startswith("http://") or url.startswith("https://"):
            # 原生接口没有「任意公网 URL 当图片」的字段，这里只跳过、不下载
            skipped_remote += 1
            continue
        match = _DATA_URL_RE.fullmatch(url)
        if match is None:
            logger.debug("Gemini 原生请求跳过一张无法解析的 data URL 图片")
            continue
        mime_type = match.group("mime").strip()
        data = match.group("data")
        if not mime_type or not data:
            logger.debug("Gemini 原生请求跳过一张缺少 MIME 或正文的 data URL 图片")
            continue
        parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
    return parts, skipped_remote


def gemini_contents(messages: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """把 OpenAI 形态消息转成 (systemInstruction, contents)

    - 开头连续的 system 合成 `systemInstruction`；之后再出现的 system 按 user 发；
    - assistant -> model，其余 -> user；
    - 相邻同角色合并，前后两个文本片段之间补 `"\\n\\n"`；
    - 文本一个字节都不改：不可信块的包裹与一次性令牌标签都要原样到上游。
    """
    system_texts: List[str] = []
    contents: List[Dict[str, Any]] = []
    skipped_remote = 0
    seen_non_system = False

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")

        if role == "system" and not seen_non_system:
            text = "".join(_text_parts(content))
            if text:
                system_texts.append(text)
            continue
        seen_non_system = True

        parts, skipped = _content_parts(content)
        skipped_remote += skipped
        if not parts:
            continue

        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            previous_parts = contents[-1]["parts"]
            next_parts = parts
            if previous_parts and next_parts and "text" in previous_parts[-1] and "text" in next_parts[0]:
                previous_parts[-1] = {"text": previous_parts[-1]["text"] + "\n\n" + next_parts[0]["text"]}
                next_parts = next_parts[1:]
            previous_parts.extend(next_parts)
        else:
            contents.append({"role": gemini_role, "parts": parts})

    if skipped_remote:
        logger.debug(f"Gemini 原生请求跳过了 {skipped_remote} 张 http(s) 图片（原生接口只接受 inlineData）")

    system_instruction: Optional[Dict[str, Any]] = None
    if system_texts:
        joined = "\n\n".join(system_texts)
        if joined:
            system_instruction = {"parts": [{"text": joined}]}

    if not contents and system_instruction is not None:
        contents = [{"role": "user", "parts": [{"text": system_instruction["parts"][0]["text"]}]}]
        system_instruction = None
    return system_instruction, contents


def _deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    """递归深合并；source 的值覆盖 target 的同名值"""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def gemini_request_body(
    messages: List[Dict[str, Any]],
    *,
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    max_tokens: Optional[int],
    stop_words: Optional[List[str]],
    presence_penalty: Optional[float],
    frequency_penalty: Optional[float],
    extra_body: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """组装原生请求体

    `generationConfig` 只放有值的采样参数；额外请求体只允许 `generationConfig`（递归深合并）、
    `safetySettings`（整体替换）、`cachedContent`（字符串），并兼容 `response_format` / `top_k`
    两种 OpenAI 写法。其余键丢弃，只把键名写进一条 WARNING（不打印值）。
    """
    system_instruction, contents = gemini_contents(messages)

    generation_config: Dict[str, Any] = {}
    sampling_fields = (
        ("temperature", temperature),
        ("topP", top_p),
        ("topK", top_k),
        ("maxOutputTokens", max_tokens),
        ("stopSequences", stop_words),
        ("presencePenalty", presence_penalty),
        ("frequencyPenalty", frequency_penalty),
    )
    for key, value in sampling_fields:
        if value is not None:
            generation_config[key] = value

    # 先深拷贝：合并不许改到调用方传进来的 dict
    extra = copy.deepcopy(extra_body) if isinstance(extra_body, dict) else {}

    dropped = sorted(key for key in extra if key not in _ALLOWED_EXTRA_KEYS)
    if dropped:
        logger.warning(f"Gemini 原生请求体丢弃不支持的键: {', '.join(dropped)}")

    extra_generation = extra.get("generationConfig")
    if isinstance(extra_generation, dict):
        _deep_merge(generation_config, extra_generation)

    # 有人把 top_k 写在额外请求体里时也映射进 generationConfig；已有 topK 就不覆盖
    if "top_k" in extra and "topK" not in generation_config:
        extra_top_k = extra["top_k"]
        if isinstance(extra_top_k, int) and not isinstance(extra_top_k, bool):
            generation_config["topK"] = extra_top_k

    # OpenAI 写法的 JSON 输出：只补 responseMimeType，不搬 response_format 本身
    response_format = extra.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in ("json_object", "json_schema"):
        generation_config.setdefault("responseMimeType", "application/json")

    body: Dict[str, Any] = {"contents": contents}
    if system_instruction is not None:
        body["systemInstruction"] = system_instruction
    if generation_config:
        body["generationConfig"] = generation_config

    safety_settings = extra.get("safetySettings")
    if isinstance(safety_settings, list):
        body["safetySettings"] = copy.deepcopy(safety_settings)
    cached_content = extra.get("cachedContent")
    if isinstance(cached_content, str):
        body["cachedContent"] = cached_content
    return body


def _valid_count(value: Any) -> Optional[int]:
    """非整数 / 负数 / bool 一律当缺失"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
    number = int(value)
    return number if number >= 0 else None


def _cached_tokens(usage: Any) -> Optional[int]:
    from .openai import _cached_input_tokens

    return _cached_input_tokens(usage)


class GeminiUsage(NamedTuple):
    """一份 usageMetadata 映射后的四项计数；`None` 表示这份响应没说这一项"""

    token_input: Optional[int]
    token_input_cached: Optional[int]
    token_output: Optional[int]
    token_total: Optional[int]


def gemini_usage(payload: Dict[str, Any]) -> GeminiUsage:
    """非流式响应的用量映射

    输出 = `candidatesTokenCount` + `thoughtsTokenCount`（缺的当 0）；总数缺了就用输入 + 输出；
    缓存命中交给 `openai._cached_input_tokens`（认 `cachedContentTokenCount`）。
    """
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return GeminiUsage(None, None, None, None)

    token_input = _valid_count(usage.get("promptTokenCount"))
    candidates = _valid_count(usage.get("candidatesTokenCount"))
    thoughts = _valid_count(usage.get("thoughtsTokenCount"))
    if candidates is None and thoughts is None:
        token_output: Optional[int] = None
    else:
        token_output = (candidates or 0) + (thoughts or 0)
    token_total = _valid_count(usage.get("totalTokenCount"))
    if token_total is None:
        token_total = (token_input or 0) + (token_output or 0)
    return GeminiUsage(token_input, _cached_tokens(usage), token_output, token_total)


class GeminiUsageAccumulator:
    """流式用量：每个事件只覆盖它真的带了的项，没带的保留前面读到的值"""

    def __init__(self) -> None:
        self.token_input = 0
        self.token_input_cached = 0
        self.token_output = 0
        self.token_total = 0
        self._candidates = 0
        self._thoughts = 0

    def update(self, payload: Dict[str, Any]) -> None:
        usage = payload.get("usageMetadata")
        if not isinstance(usage, dict):
            return
        token_input = _valid_count(usage.get("promptTokenCount"))
        if token_input is not None:
            self.token_input = token_input
        candidates = _valid_count(usage.get("candidatesTokenCount"))
        if candidates is not None:
            self._candidates = candidates
        thoughts = _valid_count(usage.get("thoughtsTokenCount"))
        if thoughts is not None:
            self._thoughts = thoughts
        if candidates is not None or thoughts is not None:
            self.token_output = self._candidates + self._thoughts
        token_total = _valid_count(usage.get("totalTokenCount"))
        if token_total is not None:
            self.token_total = token_total
        else:
            self.token_total = self.token_input + self.token_output
        cached = _cached_tokens(usage)
        if cached is not None:
            self.token_input_cached = cached


def gemini_text_and_thought(payload: Dict[str, Any]) -> Tuple[str, str]:
    """从 candidates[0].content.parts 里取 (正文, 思考)；`thought=True` 的文本进思考"""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return "", ""
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return "", ""
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return "", ""
    text_chunks: List[str] = []
    thought_chunks: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        if part.get("thought") is True:
            thought_chunks.append(text)
        else:
            text_chunks.append(text)
    return "".join(text_chunks), "".join(thought_chunks)


def gemini_finish_reason(payload: Dict[str, Any]) -> str:
    """candidates[0].finishReason 归一化；没有候选人时为空串

    `MAX_TOKENS` 在 OpenAI 兼容层写作 `length`，这里也统一成 `length`：两条接口格式
    的收尾原因必须可比较，否则 "被截断" 在一条分支上永远匹配不到现有判据。
    """
    from .openai import normalize_finish_reason

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return ""
    normalized = normalize_finish_reason(candidate.get("finishReason"))
    return "length" if normalized == "max_tokens" else normalized


def gemini_block_reason(payload: Dict[str, Any]) -> str:
    """promptFeedback.blockReason 归一化；未指定/空值回空串"""
    from .openai import normalize_finish_reason

    feedback = payload.get("promptFeedback")
    reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
    normalized = normalize_finish_reason(reason)
    if not normalized or normalized == "block_reason_unspecified":
        return ""
    return normalized


class GeminiAPIError(Exception):
    """Gemini 原生上游错误

    `status_code` 是 HTTP 状态码（或事件里 `error.code`）；上级的
    `model_capability._extract_status_code` 会读这个属性，视觉能力与预填被拒的分类因此照常生效。
    `str()` 里不含请求地址——地址与 Key 都不应该出现在任何异常文案里。
    """

    def __init__(self, status_code: Optional[int], message: str, *, status_label: str = "") -> None:
        self.status_code = status_code
        self.message = (message or "")[:2000]
        self.status_label = status_label or ""
        super().__init__(self._compose())

    def _compose(self) -> str:
        head = f"Gemini API error {self.status_code}" if self.status_code is not None else "Gemini API error"
        if self.status_label:
            head += f" ({self.status_label})"
        return f"{head}: {self.message}"


def _status_from_error_code(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def _read_response_body(response: httpx.Response, limit: int = 65536) -> bytes:
    """读错误响应体；流式响应最多读 64 KiB"""
    try:
        content = response.content
    except httpx.ResponseNotRead:
        content = None
    if content:
        return content[:limit]

    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = limit - len(chunks)
        if remaining <= 0:
            break
        chunks.extend(chunk[:remaining])
    return bytes(chunks)


async def raise_gemini_http_error(response: httpx.Response) -> None:
    """非 2xx：优先取 JSON 里的 error.message / error.status，否则回正文前 2000 字

    **不用 `response.raise_for_status()`**：httpx 的异常文案会带上请求 URL，
    而地址可能带租户参数、也可能被上游回显到正文里。
    """
    body = await _read_response_body(response)
    text = body.decode("utf-8", errors="replace")
    message = ""
    label = ""
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 - 非 JSON 的错误体按纯文本处理
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            raw_message = error.get("message")
            if isinstance(raw_message, str):
                message = raw_message
            raw_status = error.get("status")
            if isinstance(raw_status, str):
                label = raw_status
    if not message:
        message = text
    if not message:
        message = f"HTTP {response.status_code}"
    raise GeminiAPIError(response.status_code, message, status_label=label)


def raise_gemini_event_error(event: Dict[str, Any]) -> None:
    """SSE 事件里带 `error` 对象时按同样的规则抛错"""
    error = event.get("error")
    if not isinstance(error, dict):
        return
    raw_message = error.get("message")
    message = raw_message if isinstance(raw_message, str) else ""
    raw_status = error.get("status")
    label = raw_status if isinstance(raw_status, str) else ""
    raise GeminiAPIError(_status_from_error_code(error.get("code")), message, status_label=label)


async def iter_gemini_sse_events(response: httpx.Response) -> AsyncGenerator[Dict[str, Any], None]:
    """逐行读 SSE，只认 `data:` 行；`[DONE]` 与解析失败的行跳过"""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :]
        if data.startswith(" "):
            data = data[1:]
        if data.strip() == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except Exception as exc:  # noqa: BLE001 - 单行坏掉不影响后面的增量
            logger.debug(f"Gemini 流式响应里有一行无法解析，已跳过: {exc}")
            continue
        if isinstance(event, dict):
            yield event


async def list_gemini_models(
    *,
    base_url: Optional[str],
    api_key: Optional[str],
    proxy_url: Optional[str] = None,
    tenant_egress: bool,
) -> List[str]:
    """拉取原生模型列表

    `GET {root}/models`（`pageSize` 走 httpx 的 params，不手拼查询串），最多翻 10 页；
    条目带 `supportedGenerationMethods` 时只留含 `generateContent` 的。一次性客户端、用完就关，
    不进按地址缓存的连接池（地址可能直接来自请求体）。
    """
    from .openai import _create_http_client

    root = gemini_api_root(base_url)
    headers = gemini_headers(api_key)
    names: List[str] = []
    page_token = ""

    async with _create_http_client(
        proxy_url=proxy_url,
        read_timeout=60,
        write_timeout=60,
        connect_timeout=10,
        pool_timeout=10,
        tenant_egress=tenant_egress,
    ) as client:
        for _page in range(10):
            params: Dict[str, Any] = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            response = await client.get(f"{root}/models", params=params, headers=headers)
            if response.is_error:
                await raise_gemini_http_error(response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("模型列表响应格式无效")
            for item in payload.get("models") or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                methods = item.get("supportedGenerationMethods")
                if isinstance(methods, list) and "generateContent" not in methods:
                    continue
                if name.startswith("models/"):
                    name = name[len("models/") :]
                if name:
                    names.append(name)
            next_token = payload.get("nextPageToken")
            page_token = next_token if isinstance(next_token, str) else ""
            if not page_token:
                break

    return sorted(set(names))
