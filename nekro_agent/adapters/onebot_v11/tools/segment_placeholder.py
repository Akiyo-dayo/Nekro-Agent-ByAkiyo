"""OneBot 消息段的占位文本

没有专门处理的段不能静默消失：群里单发一个表情或骰子时，转换结果为空会让整条消息被丢掉，
AI 和 WebUI 都看不到。这里给每类段留一段固定文案。

占位文本只会由固定文字和经过校验的值组成，绝不拼接标题、摘要、文件名、昵称这类发送方
可以随意填写的内容：纯文本段原样进入提示词，拼接任意外部字符串等于开一条提示词注入通道。
"""

import re
from collections.abc import Mapping
from typing import Any, Optional

# 引用回复由 get_message_reply_info 从 original_message 另取，这里不重复留占位。
# keyboard 是官方机器人 markdown 消息附带的按钮段，本身没有独立信息，
# 留占位只会让每条这类消息多一行「暂不支持」。
IGNORED_SEGMENT_TYPES = frozenset({"reply", "keyboard"})

SEGMENT_PLACEHOLDERS: dict[str, str] = {
    "face": "[表情]",
    "mface": "[表情包]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
    "shake": "[窗口抖动]",
    "music": "[音乐分享]",
    "contact": "[名片分享]",
    "location": "[位置分享]",
    "share": "[链接分享]",
    "xml": "[XML卡片]",
    "miniapp": "[小程序]",
    "onlinefile": "[在线文件]",
    "flashtransfer": "[闪传文件]",
    "markdown": "[Markdown消息]",
}
UNSUPPORTED_SEGMENT_TEXT = "[暂不支持的消息]"

# 表情包的 summary 由发送方填写，只接受 1~8 个汉字/字母/数字（可带一层方括号）
MFACE_SUMMARY_PATTERN = re.compile(r"^\[?([\u4e00-\u9fffA-Za-z0-9]{1,8})\]?$")
# 未知类型名只允许小写字母开头、总长不超过 24，避免把发送方的任意长文本带进提示词
UNKNOWN_SEGMENT_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,23}")


def describe_segment(seg_type: str, data: Mapping[str, Any] | None) -> Optional[str]:
    """把没有专门处理的段变成占位文本；不需要占位时返回 None"""
    values: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
    type_name = seg_type if isinstance(seg_type, str) else ""

    if type_name in IGNORED_SEGMENT_TYPES:
        return None

    if type_name == "dice":
        result = str(values.get("result")).strip()
        if result in {"1", "2", "3", "4", "5", "6"}:
            return f"[骰子：{result}]"
        return "[骰子]"

    if type_name == "mface":
        summary = values.get("summary")
        if isinstance(summary, str):
            matched = MFACE_SUMMARY_PATTERN.fullmatch(summary)
            if matched:
                return f"[表情包：{matched.group(1)}]"
        return "[表情包]"

    if type_name == "contact":
        contact_type = values.get("type")
        if contact_type == "qq":
            return "[推荐好友]"
        if contact_type == "group":
            return "[推荐群聊]"
        return "[名片分享]"

    if type_name in SEGMENT_PLACEHOLDERS:
        return SEGMENT_PLACEHOLDERS[type_name]

    if type_name and UNKNOWN_SEGMENT_TYPE_PATTERN.fullmatch(type_name):
        return f"[暂不支持的消息：{type_name}]"
    return UNSUPPORTED_SEGMENT_TEXT
