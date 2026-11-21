"""酒馆对话补全预设的解析（纯函数，不查库）

输入是 SillyTavern「对话补全预设」JSON 解析出的对象，输出是按发送顺序排好的段落、
预设自带正则转成的清理规则，以及导入报告。这里不碰数据库、不展开宏、不决定某一段
最终发什么——排布在 :mod:`nekro_agent.services.tavern.rp_layout` 里做。

## 这一版明确不做

- 预设自带的正则只保存、不生效；报告里会写明。
- 预填（最后一段 assistant）不发送，只在预览的元数据里给出文字。
- 聊天记录仍是一条 user 消息，不拆成一条条；聊天记录之后的段也不单独成消息。
- 不导入采样参数（温度、top_p 等），模型组说了算；报告里只列出来供参考。
- 不支持编辑段落正文与调整顺序，段落开关可以改。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .cleanup import rule_from_st_script
from .macros import SUPPORTED_MACROS

# 上限。超了整份预设都不收：解析到一半再截断会让保存下来的顺序与原文对不上。
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SEGMENTS = 400
MAX_SEGMENT_CHARS = 100_000
MAX_ENABLED_CHARS = 300_000

# 占位段：正文由 NA 填，原文件里的 content 不保存。`marker: true` 的段同样按占位处理。
PLACEHOLDER_IDENTIFIERS = frozenset(
    {
        "chatHistory",
        "charDescription",
        "charPersonality",
        "scenario",
        "personaDescription",
        "dialogueExamples",
        "worldInfoBefore",
        "worldInfoAfter",
    },
)

# ST 的 role 既可能是名字，也可能是 0 / 1 / 2；认不出时按 system。
_ROLE_BY_NAME = {"system": "system", "user": "user", "assistant": "assistant"}
_ROLE_BY_NUMBER = {0: "system", 1: "user", 2: "assistant"}

# 抽宏名：只认字母或下划线开头，`{{// 注释}}` 这类以 `/` 开头的内容不会被抽到。
_MACRO_NAME = re.compile(r"\{\{\s*([A-Za-z_][\w]*)")

# 报告里列出的采样参数，顺序固定，方便对照。
_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "frequency_penalty",
    "presence_penalty",
    "openai_max_tokens",
)


class RpPresetRejected(ValueError):
    """这不是一份可用的对话补全预设；消息是可直接展示给管理员的中文原因"""


@dataclass(frozen=True)
class RpSegment:
    """预设里的一段在 NA 里的规范化形状

    `forbid_overrides` 不在 ST 的公开字段说明里逐字列出，但它与 `main` / `jailbreak`
    的卡片覆盖规则直接相关，解析时一并保存；旧数据或手工构造时可以省略。
    """

    identifier: str
    name: str
    role: str
    content: str
    marker: bool
    position: str
    depth: int
    injection_order: int
    enabled: bool
    forbid_overrides: bool = False


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _as_sequence(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _normalize_role(value: Any) -> str:
    if isinstance(value, bool):
        return "system"
    if isinstance(value, int):
        return _ROLE_BY_NUMBER.get(value, "system")
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.isdigit():
            return _ROLE_BY_NUMBER.get(int(lowered), "system")
        return _ROLE_BY_NAME.get(lowered, "system")
    return "system"


def _normalize_position(value: Any) -> str:
    """ST 的 `injection_position`：0 = relative，1 = in_chat；也接受酒馆前端导出的名字"""
    if isinstance(value, str) and value.strip().lower() in {"relative", "in_chat"}:
        return value.strip().lower()
    return "in_chat" if _as_int(value, 0) == 1 else "relative"


def _choose_order(prompt_order: Sequence[Any]) -> list[Mapping[str, Any]]:
    """取 `order` 最长的那一份顺序表；没有一份可用的就拒绝"""
    best: list[Mapping[str, Any]] | None = None
    for entry in prompt_order:
        container = _as_mapping(entry)
        if container is None:
            continue
        raw_order = _as_sequence(container.get("order"))
        if raw_order is None:
            continue
        items = [item for item in raw_order if isinstance(item, Mapping)]
        if best is None or len(items) > len(best):
            best = items
    if best is None:
        raise RpPresetRejected("这不是对话补全预设（缺 prompts / prompt_order）")
    return best


def _segment_from_prompt(
    prompt: Mapping[str, Any],
    *,
    enabled: bool,
    identifier: str,
) -> RpSegment:
    marker = bool(prompt.get("marker")) or identifier in PLACEHOLDER_IDENTIFIERS
    content = "" if marker else _text(prompt.get("content"))
    position = _normalize_position(prompt.get("injection_position"))
    depth = max(0, _as_int(prompt.get("injection_depth"), 0)) if position == "in_chat" else 0
    return RpSegment(
        identifier=identifier,
        name=_text(prompt.get("name")),
        role=_normalize_role(prompt.get("role")),
        content=content,
        marker=marker,
        position=position,
        depth=depth,
        injection_order=_as_int(prompt.get("injection_order"), 100),
        enabled=enabled,
        forbid_overrides=bool(prompt.get("forbid_overrides")),
    )


def _size_guard(raw: Mapping[str, Any]) -> None:
    """文件上限：按原对象重新序列化后的 UTF-8 字节数判断

    调用方通常已经按上传字节数挡过一次，这里再判一次是为了让纯函数自己也能守住上限：
    直接拿对象调用解析器时，不该因为绕过路由就收下一份超大的预设。
    """
    try:
        size = len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return
    if size > MAX_FILE_BYTES:
        raise RpPresetRejected(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB")


def _limits_guard(segments: Sequence[RpSegment]) -> None:
    if len(segments) > MAX_SEGMENTS:
        raise RpPresetRejected(f"段落数超过上限 {MAX_SEGMENTS} 段（这份有 {len(segments)} 段）")
    enabled_chars = 0
    for segment in segments:
        if len(segment.content) > MAX_SEGMENT_CHARS:
            raise RpPresetRejected(
                f"段落「{segment.name or segment.identifier}」正文超过 {MAX_SEGMENT_CHARS} 字",
            )
        if segment.enabled:
            enabled_chars += len(segment.content)
    if enabled_chars > MAX_ENABLED_CHARS:
        raise RpPresetRejected(f"启用段落的正文合计超过 {MAX_ENABLED_CHARS} 字（这份有 {enabled_chars} 字）")


def _regex_rules(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把预设自带的正则逐条转成清理规则；形状不对的条目跳过"""
    extensions = _as_mapping(raw.get("extensions"))
    scripts = _as_sequence(extensions.get("regex_scripts")) if extensions is not None else None
    if not scripts:
        return []
    rules: list[dict[str, Any]] = []
    for script in scripts:
        mapping = _as_mapping(script)
        if mapping is None:
            continue
        try:
            rules.append(dict(rule_from_st_script(mapping)))
        except Exception:  # noqa: BLE001 - 单条正则转不动不该让整份预设导入失败
            continue
    return rules


def _split_counts(segments: Sequence[RpSegment]) -> tuple[int, int, int, int, int]:
    """返回（前置段数、前置字数、后置段数、后置字数、深度段数）；只算启用的段"""
    seen_boundary = False
    pre_count = pre_chars = post_count = post_chars = depth_count = 0
    for segment in segments:
        if not segment.enabled:
            continue
        if segment.position == "in_chat":
            depth_count += 1
            continue
        if segment.identifier == "chatHistory":
            seen_boundary = True
            continue
        if seen_boundary:
            post_count += 1
            post_chars += len(segment.content)
        else:
            pre_count += 1
            pre_chars += len(segment.content)
    return pre_count, pre_chars, post_count, post_chars, depth_count


def _notes(
    segments: Sequence[RpSegment],
    regex_count: int,
    raw: Mapping[str, Any],
) -> list[dict[str, str]]:
    """导入报告：只看启用的段，数字与运行时排布同一口径"""
    enabled = [segment for segment in segments if segment.enabled]
    notes: list[dict[str, str]] = [
        {"kind": "summary", "text": f"共 {len(segments)} 段，启用 {len(enabled)} 段"},
    ]
    pre_count, pre_chars, post_count, post_chars, depth_count = _split_counts(segments)
    notes.append({"kind": "pre_split", "text": f"聊天记录之前 {pre_count} 段、{pre_chars} 字"})
    notes.append({"kind": "post_split", "text": f"聊天记录之后 {post_count} 段、{post_chars} 字"})
    notes.append({"kind": "in_chat", "text": f"按深度插入 {depth_count} 段"})

    seen_boundary = False
    pre_roles = 0
    tail_assistants = 0
    for segment in enabled:
        if segment.position == "in_chat":
            continue
        if segment.identifier == "chatHistory":
            seen_boundary = True
            continue
        if not seen_boundary:
            if segment.role in ("user", "assistant"):
                pre_roles += 1
        elif segment.role == "assistant":
            tail_assistants += 1
    if pre_roles:
        notes.append(
            {
                "kind": "pre_roles",
                "text": f"前置段里有 {pre_roles} 段角色是 user / assistant，这一版按系统文本合并",
            },
        )
    if tail_assistants:
        notes.append(
            {
                "kind": "tail_assistant",
                "text": (
                    f"聊天记录之后有 {tail_assistants} 段 assistant："
                    "最后一段 assistant 视为预填，这一版不发送；其余按文本并入"
                ),
            },
        )

    used_text = "\n".join(segment.content for segment in enabled if not segment.marker)
    used_macros = sorted({match.group(1).lower() for match in _MACRO_NAME.finditer(used_text)})
    unknown = [name for name in used_macros if name not in SUPPORTED_MACROS]
    if unknown:
        notes.append({"kind": "unsupported_macros", "text": f"NA 不认的宏：{'、'.join(unknown)}（原样保留）"})

    sampling = [
        f"{key}={raw[key]}"
        for key in _SAMPLING_KEYS
        if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool)
    ]
    if sampling:
        notes.append(
            {
                "kind": "sampling",
                "text": f"采样参数：{'、'.join(sampling)}（不导入，模型组说了算）",
            },
        )

    if regex_count:
        notes.append(
            {"kind": "regex", "text": f"附带 {regex_count} 条正则，已保存，这一版不生效"},
        )
    return notes


def parse_preset(raw: Mapping[str, Any]) -> tuple[list[RpSegment], list[dict[str, Any]], list[dict[str, str]]]:
    """解析一份酒馆对话补全预设

    Args:
        raw: 原文件 JSON 解析出的对象。

    Returns:
        （按发送顺序排好的段落, 清理规则, 报告条目）。段落里既有启用的也有停用的，
        停用的排在它原本的位置上，运行时按 `enabled` 过滤。

    Raises:
        RpPresetRejected: 不是预设文件、缺 `chatHistory`、或超过任一上限；消息可直接展示。
    """
    if not isinstance(raw, Mapping):
        raise RpPresetRejected("这不是对话补全预设（顶层不是对象）")
    _size_guard(raw)

    prompts_raw = _as_sequence(raw.get("prompts"))
    prompt_order = _as_sequence(raw.get("prompt_order"))
    if prompts_raw is None or not prompt_order:
        raise RpPresetRejected("这不是对话补全预设（缺 prompts / prompt_order）")

    prompts: dict[str, Mapping[str, Any]] = {}
    prompt_identifiers: list[str] = []
    for item in prompts_raw:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        identifier = _text(mapping.get("identifier")).strip()
        if not identifier:
            continue
        if identifier not in prompts:
            prompt_identifiers.append(identifier)
        prompts[identifier] = mapping

    order = _choose_order(prompt_order)
    seen: set[str] = set()
    segments: list[RpSegment] = []
    for item in order:
        identifier = _text(item.get("identifier")).strip()
        if not identifier or identifier in seen or identifier not in prompts:
            continue
        seen.add(identifier)
        segments.append(
            _segment_from_prompt(
                prompts[identifier],
                enabled=bool(item.get("enabled")),
                identifier=identifier,
            ),
        )

    # `prompts` 里有、顺序表里没有的段排在最后且关闭；保存下来让管理员能在详情里看到。
    for identifier in prompt_identifiers:
        if identifier in seen:
            continue
        seen.add(identifier)
        segments.append(_segment_from_prompt(prompts[identifier], enabled=False, identifier=identifier))

    if not any(segment.identifier == "chatHistory" for segment in segments):
        raise RpPresetRejected("这不是对话补全预设（缺 chatHistory 占位段，无法判断哪些段在聊天记录之后）")

    _limits_guard(segments)
    rules = _regex_rules(raw)
    notes = _notes(segments, len(rules), raw)
    return segments, rules, notes


def segments_from_stored(data: Any) -> list[RpSegment]:
    """把数据库里存的段落列表还原成 :class:`RpSegment`

    只做形状容错：历史数据、手工改库、或以后新增字段时的缺键都按默认值处理，不让一轮
    对话因此失败。正文与开关原样取用。
    """
    items = _as_sequence(data) or []
    segments: list[RpSegment] = []
    for item in items:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        identifier = _text(mapping.get("identifier")).strip()
        if not identifier:
            continue
        position = _normalize_position(mapping.get("position"))
        segments.append(
            RpSegment(
                identifier=identifier,
                name=_text(mapping.get("name")),
                role=_normalize_role(mapping.get("role")),
                content=_text(mapping.get("content")),
                marker=bool(mapping.get("marker")) or identifier in PLACEHOLDER_IDENTIFIERS,
                position=position,
                depth=max(0, _as_int(mapping.get("depth"), 0)) if position == "in_chat" else 0,
                injection_order=_as_int(mapping.get("injection_order"), 100),
                enabled=bool(mapping.get("enabled")),
                forbid_overrides=bool(mapping.get("forbid_overrides")),
            ),
        )
    return segments


def segment_as_dict(segment: RpSegment) -> dict[str, Any]:
    """段的 JSON 形状；存表与接口返回共用一份字段表"""
    return {
        "identifier": segment.identifier,
        "name": segment.name,
        "role": segment.role,
        "content": segment.content,
        "marker": segment.marker,
        "position": segment.position,
        "depth": segment.depth,
        "injection_order": segment.injection_order,
        "enabled": segment.enabled,
        "forbid_overrides": segment.forbid_overrides,
    }


__all__ = [
    "MAX_ENABLED_CHARS",
    "MAX_FILE_BYTES",
    "MAX_SEGMENTS",
    "MAX_SEGMENT_CHARS",
    "PLACEHOLDER_IDENTIFIERS",
    "RpPresetRejected",
    "RpSegment",
    "parse_preset",
    "segment_as_dict",
    "segments_from_stored",
]
