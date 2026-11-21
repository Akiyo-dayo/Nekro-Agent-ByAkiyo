"""共享到云端时的酒馆卡信封

NekroCloud 的人设接口只有一个字符串扩展位 `extData`，而酒馆卡需要带整张卡、完整
正文与三项 NA 私有字段。这里把这三样装进一个带版本号的对象，下载与同步时再按同一
套规则拆开；版本号与顶层键都在，才能把「不是信封的老数据」与「信封坏了」分开：
前者照旧处理，后者要回退并提示。

信封里的卡片与正文都按不可信输入重新解析，不能因为来自云端就跳过导入时的上限与
清洗；`prev_ext_data` 只用于把共享前的原值还回去，避免下载后本地多出一份信封。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .limits import MAX_CARD_JSON_BYTES, MAX_JSON_DEPTH

ENVELOPE_KEY = "nekro_tavern"
ENVELOPE_VERSION = 1
# 云端 `content` 字段的本地上限；与 `systems/cloud/schemas/preset.py` 的 PresetCreate 一致。
CLOUD_CONTENT_MAX = 5000


@dataclass
class Envelope:
    """拆开的信封内容

    `card` 是导出时的整张卡对象，`content` 是完整正文，`na` 三项是卡片规范里没有的
    NA 私有列；`prev_ext_data` 是共享前 `presets.ext_data` 的原值。
    """

    card: dict[str, Any]
    content: str
    prefill: str
    macros_enabled: bool
    greeting_pick: str
    prev_ext_data: Any


def _is_empty_ext_data(value: Any) -> bool:
    """判断扩展位是不是空的：None、空串、空对象与空数组都不写进信封"""
    return value is None or value == "" or value == {} or value == []


def build_envelope(
    card_obj: dict,
    *,
    content: str,
    prefill: str,
    macros_enabled: bool,
    greeting_pick: str,
    prev_ext_data: Any,
) -> str:
    """把卡片、完整正文与 NA 三项打包成云端扩展字段

    `prev_ext_data` 为空时不写这个键：下载时缺键与显式空值都回落成空对象，少一个键
    让信封更小，也避免把共享前的空值当成一次真实数据。
    """
    payload: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "card": card_obj,
        "content": content,
        "na": {
            "prefill": prefill,
            "macros_enabled": macros_enabled,
            "greeting_pick": greeting_pick,
        },
    }
    if not _is_empty_ext_data(prev_ext_data):
        payload["prev_ext_data"] = prev_ext_data
    return json.dumps({ENVELOPE_KEY: payload}, ensure_ascii=False)


def cloud_content_field(content: str) -> str:
    """云端 `content` 栏只放得下 5000 字，超长时保留前 4999 字加省略号

    市场列表只展示这一栏，完整正文在信封里；截断只影响列表页的预览。
    """
    if len(content) <= CLOUD_CONTENT_MAX:
        return content
    return content[: CLOUD_CONTENT_MAX - 1] + "…"


def _depth_exceeded(value: Any) -> bool:
    """迭代计算 JSON 深度；递归版会在极深对象上先栈溢出，拒绝原因就说不清了"""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return True
        if isinstance(node, dict):
            for item in node.values():
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))
    return False


def parse_envelope(raw: Any) -> tuple[Envelope | None, str]:
    """尝试把云端扩展字段拆成信封

    返回 `(信封, "")` 表示成功；`(None, "")` 表示这份数据压根不是信封，调用方应走
    改造前的逻辑；`(None, 原因)` 表示它看起来是信封但坏了，调用方要回退并提示。
    原因只描述结构问题，不带扩展字段原文。
    """
    if raw is None:
        return None, ""
    if isinstance(raw, str):
        if not raw.strip():
            # 存量 `ext_data` 常见的空串；它和空对象一样只是「没有信封」，不是坏数据。
            return None, ""
        if len(raw.encode("utf-8")) > MAX_CARD_JSON_BYTES:
            return None, "扩展数据超过卡片大小上限"
        try:
            raw = json.loads(raw)
        except RecursionError:
            return None, f"JSON 嵌套超过 {MAX_JSON_DEPTH} 层"
        except ValueError:
            return None, "扩展数据不是合法的 JSON"
    if not isinstance(raw, dict):
        return None, ""
    if ENVELOPE_KEY not in raw:
        return None, ""
    if _depth_exceeded(raw):
        return None, f"JSON 嵌套超过 {MAX_JSON_DEPTH} 层"

    payload = raw[ENVELOPE_KEY]
    if not isinstance(payload, dict):
        return None, "信封结构不正确"

    version = payload.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version != ENVELOPE_VERSION:
        return None, "信封版本不认识"

    card = payload.get("card")
    if not isinstance(card, dict):
        return None, "信封里缺少卡片对象"

    content = payload.get("content")
    if not isinstance(content, str):
        return None, "信封里的人设正文不是字符串"

    na = payload.get("na", {})
    if not isinstance(na, dict):
        return None, "信封里的 NA 字段不是对象"
    prefill = na.get("prefill", "")
    if not isinstance(prefill, str):
        return None, "信封里的预填文字不是字符串"
    macros_enabled = na.get("macros_enabled", True)
    if not isinstance(macros_enabled, bool):
        return None, "信封里的宏开关不是布尔值"
    greeting_pick = na.get("greeting_pick", "first")
    if not isinstance(greeting_pick, str):
        return None, "信封里的开场白挑选方式不是字符串"
    if greeting_pick not in ("first", "random"):
        greeting_pick = "first"

    return (
        Envelope(
            card=card,
            content=content,
            prefill=prefill,
            macros_enabled=macros_enabled,
            greeting_pick=greeting_pick,
            prev_ext_data=payload.get("prev_ext_data"),
        ),
        "",
    )


__all__ = [
    "CLOUD_CONTENT_MAX",
    "ENVELOPE_KEY",
    "ENVELOPE_VERSION",
    "Envelope",
    "build_envelope",
    "cloud_content_field",
    "parse_envelope",
]
