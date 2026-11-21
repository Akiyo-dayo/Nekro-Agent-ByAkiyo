"""按导入的酒馆预设排布这一轮的系统块、聊天记录之后的段与深度段（纯函数）

输入是已经规范化好的段落、人设卡字段、已经渲染好的世界书块与后置指令槽位，以及这一轮
的宏展开函数；输出是一整块替换人设节的系统文本、聊天记录之后的文本、按深度插入的
块，以及给预览用的明细。这里不查库、不拼消息、不加任何标题或说明——NA 不往里添一个字。

## 世界书只放一处

预设里启用的 `worldInfoAfter` 优先，否则 `worldInfoBefore`；两个都没启用时接在最后
一个人设类占位段之后，连人设类占位段都没有就接在系统块末尾。另一个世界书占位段给空。

## 聊天记录之后的段

最后一段启用的后置段若是 assistant，它的正文只作为预填带出预览，这一版不发送；其余
后置段按顺序用空行连接。`jailbreak` 段由后置指令槽位覆盖，槽位或段里的 `{{original}}`
换成预设原文；预设里没有启用的 `jailbreak` 时，槽位文本追加在最后。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .rp_preset import RpSegment

_ORIGINAL = re.compile(r"\{\{\s*original\s*\}\}", re.IGNORECASE)

_WORLD_INFO_IDS = frozenset({"worldInfoBefore", "worldInfoAfter"})
# 世界书没有占位段可落时，接在这些占位段之后。
_PERSONA_IDS = frozenset({"charDescription", "charPersonality", "scenario"})

_FIELD_BY_PLACEHOLDER = {
    "charDescription": "description",
    "charPersonality": "personality",
    "scenario": "scenario",
    "dialogueExamples": "mes_example",
}


@dataclass(frozen=True)
class RpLayout:
    """一轮排布的结果

    `system_block` 原样替换人设节（世界书块已经在里面）；`tail_text` 拼在聊天记录之后；
    `depth_blocks` 按 `injection_order` 升序给深度注入；`prefill_text` 这一版只带出去
    给预览看，不发送。`used` 是真正产出文字的段，`where` 取 `system` / `tail` /
    `depth:<n>`。
    """

    system_block: str
    tail_text: str
    depth_blocks: tuple[tuple[int, str], ...]
    prefill_text: str
    used: tuple[dict[str, Any], ...]


def _expand_text(expand: Callable[[str], str], text: str) -> str:
    """展开一段文字并去掉首尾换行；只有空白时返回空串"""
    expanded = expand(text).strip("\n")
    return "" if not expanded.strip() else expanded


def _fill_placeholder(segment: RpSegment, fields: Mapping[str, str]) -> str:
    """占位段填哪一份文字（未展开）"""
    field_name = _FIELD_BY_PLACEHOLDER.get(segment.identifier)
    if field_name is not None:
        return str(fields.get(field_name, "") or "")
    # personaDescription 与其它 marker 段这一版没有来源，留空。
    return ""


def _substitute_original(template: str, original: str) -> str:
    return _ORIGINAL.sub(lambda _match: original, template)


def _with_piece(
    pieces: list[tuple[str, str, str]],
    segment: RpSegment,
    text: str,
) -> None:
    """非空文字才成为一块；`text` 已经 strip 过换行"""
    if text:
        pieces.append((segment.identifier, segment.name, text))


def build_layout(
    segments: Sequence[RpSegment],
    *,
    fields: Mapping[str, str],
    worldbook_block: str,
    post_history_slot: str,
    expand: Callable[[str], str],
) -> RpLayout:
    """按预设顺序排布一轮的文字

    Args:
        segments: 规范化后的段落（含停用的段），顺序就是发送顺序。
        fields: 人设卡字段 `system / description / personality / scenario / mes_example`；
            没有卡时 `description` 是人设正文，其余为空。
        worldbook_block: 已经渲染好的「人设后」世界书块，含它自己的不可信包裹；可能为空。
        post_history_slot: 现有后置指令槽位解析出的文本；可能为空。
        expand: 这一轮的宏展开函数。

    Returns:
        见 :class:`RpLayout`。
    """
    enabled = [segment for segment in segments if segment.enabled]
    depth_segments = [segment for segment in enabled if segment.position == "in_chat"]
    boundary = next(
        (index for index, segment in enumerate(enabled) if segment.identifier == "chatHistory"),
        len(enabled),
    )
    pre_segments = [
        segment
        for segment in enabled[:boundary]
        if segment.position != "in_chat" and segment.identifier != "chatHistory"
    ]
    post_segments = [
        segment
        for segment in enabled[boundary + 1 :]
        if segment.position != "in_chat" and segment.identifier != "chatHistory"
    ]

    used: list[dict[str, Any]] = []
    pre_pieces: list[tuple[str, str, str]] = []
    # 世界书只放一处：worldInfoAfter 优先，否则 worldInfoBefore；两个占位段都没有时，
    # 接在最后一个人设类占位段之后，连人设类占位段都没有就接在系统块末尾。
    worldbook_target = (
        "worldInfoAfter"
        if any(segment.identifier == "worldInfoAfter" for segment in pre_segments)
        else "worldInfoBefore"
        if any(segment.identifier == "worldInfoBefore" for segment in pre_segments)
        else ""
    )
    persona_anchor = next(
        (
            index
            for index in range(len(pre_segments) - 1, -1, -1)
            if pre_segments[index].identifier in _PERSONA_IDS
        ),
        None,
    )

    def append_worldbook(identifier: str, name: str) -> None:
        if not worldbook_block:
            return
        pre_pieces.append((identifier, name, worldbook_block))
        used.append(
            {
                "identifier": identifier,
                "name": name,
                "where": "system",
                "chars": len(worldbook_block),
            },
        )

    for index, segment in enumerate(pre_segments):
        original = segment.content
        text = ""
        if segment.identifier in _WORLD_INFO_IDS:
            text = worldbook_block if segment.identifier == worldbook_target else ""
        elif segment.identifier == "main" and str(fields.get("system", "") or "") and not segment.forbid_overrides:
            text = _expand_text(expand, _substitute_original(str(fields.get("system", "") or ""), original))
        elif segment.marker:
            text = _expand_text(expand, _fill_placeholder(segment, fields))
        else:
            text = _expand_text(expand, original)
        _with_piece(pre_pieces, segment, text)
        if text:
            used.append(
                {
                    "identifier": segment.identifier,
                    "name": segment.name,
                    "where": "system",
                    "chars": len(text),
                },
            )
        if not worldbook_target and worldbook_block and index == persona_anchor:
            append_worldbook(
                "worldInfoAfter"
                if any(item.identifier == "worldInfoAfter" for item in pre_segments)
                else "worldInfoBefore",
                "World Info",
            )

    if not worldbook_target and worldbook_block and persona_anchor is None:
        append_worldbook(
            "worldInfoAfter"
            if any(item.identifier == "worldInfoAfter" for item in pre_segments)
            else "worldInfoBefore",
            "World Info",
        )

    system_block = "\n".join(text for _identifier, _name, text in pre_pieces if text)

    depth_pieces: list[tuple[int, str]] = []
    for segment in sorted(depth_segments, key=lambda item: item.injection_order):
        text = _expand_text(expand, segment.content)
        if not text:
            continue
        depth = max(0, int(segment.depth))
        depth_pieces.append((depth, text))
        used.append(
            {
                "identifier": segment.identifier,
                "name": segment.name,
                "where": f"depth:{depth}",
                "chars": len(text),
            },
        )

    prefill_text = ""
    tail_pieces: list[tuple[str, str, str]] = []
    slot_used_in_place = False
    slot_append = False
    has_jailbreak = any(segment.identifier == "jailbreak" for segment in post_segments)
    last_index = len(post_segments) - 1
    for index, segment in enumerate(post_segments):
        if index == last_index and segment.role == "assistant":
            prefill_text = _expand_text(expand, segment.content)
            continue
        if segment.identifier == "jailbreak":
            if post_history_slot:
                if segment.forbid_overrides:
                    slot_append = True
                    text = _expand_text(expand, segment.content)
                else:
                    text = _expand_text(expand, _substitute_original(post_history_slot, segment.content))
                    slot_used_in_place = True
            else:
                text = _expand_text(expand, segment.content)
        else:
            text = _expand_text(expand, segment.content)
        _with_piece(tail_pieces, segment, text)
        if not text:
            continue
        used.append(
            {
                "identifier": segment.identifier,
                "name": segment.name,
                "where": "tail",
                "chars": len(text),
            },
        )

    if post_history_slot and (not has_jailbreak or slot_append or not slot_used_in_place):
        text = post_history_slot.strip("\n")
        if text:
            tail_pieces.append(("post_history", "后置指令", text))
            used.append(
                {
                    "identifier": "post_history",
                    "name": "后置指令",
                    "where": "tail",
                    "chars": len(text),
                },
            )

    tail_text = "\n\n".join(text for _identifier, _name, text in tail_pieces if text)
    return RpLayout(
        system_block=system_block,
        tail_text=tail_text,
        depth_blocks=tuple(depth_pieces),
        prefill_text=prefill_text,
        used=tuple(used),
    )


__all__ = ["RpLayout", "build_layout"]
