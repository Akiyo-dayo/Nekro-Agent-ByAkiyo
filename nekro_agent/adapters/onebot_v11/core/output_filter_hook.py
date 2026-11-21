"""OneBot V11 出站过滤钩子

第 1 层收口（通用发送与命令输出）覆盖不到插件直接调用的发送接口，比如
`bot.call_api("send_group_msg", ...)`、`bot.send(...)` 与命令增强输出的合并转发。
这里挂在 `Bot.call_api` 的 `on_calling_api` 上，在请求真正发出前原地改写 `data`：
NoneBot 会把同一个 dict 先交给钩子、再交给 `_call_api`，改它就是改了要发出去的内容。

第 1 层已经过滤过的出站由 `outbound_filtered` 上下文标记，钩子看到标记就直接返回，
否则同一条消息会被过滤两遍（替换文字本身也可能再次命中规则）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from nekro_agent.adapters.onebot_v11.core.channel import ADAPTER_KEY, build_chat_key
from nekro_agent.adapters.onebot_v11.core.instance import lookup_cached_instance_key
from nekro_agent.adapters.onebot_v11.core.self_sent import channel_of_call
from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services.output_filter.engine import filter_text
from nekro_agent.services.output_filter.service import (
    FilterSettings,
    is_outbound_filtered,
    settings_for_chat,
    settings_for_instance,
    settings_from_config,
)
from nekro_agent.services.output_filter.stats import record_hits_for_instance

logger = get_sub_logger("output_filter")

_MESSAGE_APIS = frozenset({"send_msg", "send_group_msg", "send_private_msg"})
_FORWARD_APIS = frozenset({"send_forward_msg", "send_group_forward_msg", "send_private_forward_msg"})
_AI_RECORD_APIS = frozenset({"send_group_ai_record"})
FILTERED_APIS = _MESSAGE_APIS | _FORWARD_APIS | _AI_RECORD_APIS

# 卡片类段：命中后整段换成 text 段，不再尝试保留原卡片结构
_CARD_SEGMENT_TYPES = frozenset({"json", "xml", "markdown", "music", "share"})
# 合并转发 node 的递归深度上限；超过之后不再往 content 里走
NODE_DEPTH_LIMIT = 5


def _segment_type(segment: Any) -> str:
    if isinstance(segment, MessageSegment):
        return str(segment.type)
    if isinstance(segment, dict):
        return str(segment.get("type") or "")
    return ""


def _segment_data(segment: Any) -> Dict[str, Any]:
    if isinstance(segment, MessageSegment):
        data = segment.data
        return data if isinstance(data, dict) else {}
    if isinstance(segment, dict):
        data = segment.get("data")
        if isinstance(data, dict):
            return data
    return {}


def _as_message_segment(segment: Any) -> MessageSegment:
    if isinstance(segment, MessageSegment):
        return segment
    return MessageSegment(_segment_type(segment), dict(_segment_data(segment)))


def _collect_strings(value: Any) -> List[str]:
    """收集嵌套结构里的全部字符串值，卡片过滤把它们拼成一段文字来匹配"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        collected: List[str] = []
        for item in value.values():
            collected.extend(_collect_strings(item))
        return collected
    if isinstance(value, (list, tuple)):
        collected = []
        for item in value:
            collected.extend(_collect_strings(item))
        return collected
    return []


def _filter_segment(
    segment: Any,
    settings: FilterSettings,
    depth: int,
) -> Tuple[Any, Counter[str], bool]:
    """过滤单个消息段，返回 (段, 命中计数, 是否有变化)"""
    segment_type = _segment_type(segment)
    data = _segment_data(segment)

    if segment_type == "text":
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return segment, Counter(), False
        result = filter_text(text, settings.rules, settings.replacement)
        if not result.hit:
            return segment, Counter(), False
        new_data = dict(data)
        new_data["text"] = result.text
        return MessageSegment("text", new_data), result.hits, True

    if segment_type in _CARD_SEGMENT_TYPES:
        combined = "\n".join(_collect_strings(data))
        if not combined:
            return segment, Counter(), False
        result = filter_text(combined, settings.rules, settings.replacement)
        if not result.hit:
            return segment, Counter(), False
        # 卡片改成纯文本：只替换卡片内部某个字段会留下一张仍然泄题的卡片
        return MessageSegment("text", {"text": settings.replacement}), result.hits, True

    if segment_type == "node":
        content = data.get("content")
        if content is None or depth >= NODE_DEPTH_LIMIT:
            return segment, Counter(), False
        new_content, hits, changed = _filter_message_value(content, settings, depth + 1)
        if not changed:
            return segment, hits, False
        new_data = dict(data)
        new_data["content"] = new_content
        return {"type": "node", "data": new_data}, hits, True

    # 图片、@、表情、回复、语音等原样保留
    return segment, Counter(), False


def _filter_segments(
    segments: List[Any],
    settings: FilterSettings,
    depth: int,
) -> Tuple[List[Any], Counter[str], bool]:
    filtered: List[Any] = []
    hits: Counter[str] = Counter()
    changed = False
    for segment in segments:
        new_segment, segment_hits, segment_changed = _filter_segment(segment, settings, depth)
        filtered.append(new_segment)
        hits.update(segment_hits)
        changed = changed or segment_changed
    return filtered, hits, changed


def _filter_message_value(
    message: Any,
    settings: FilterSettings,
    depth: int,
) -> Tuple[Any, Counter[str], bool]:
    """过滤一个 message 值（str / Message / MessageSegment / list / dict）

    没有变化时返回原对象；有变化时统一返回 `Message`。
    """
    if isinstance(message, str):
        segments = list(Message(message))
    elif isinstance(message, Message):
        segments = list(message)
    elif isinstance(message, MessageSegment):
        segments = [message]
    elif isinstance(message, dict):
        segments = [message]
    elif isinstance(message, (list, tuple)):
        segments = list(message)
    else:
        return message, Counter(), False

    filtered, hits, changed = _filter_segments(segments, settings, depth)
    if not changed:
        return message, hits, False
    if not filtered:
        return message, hits, False
    return Message([_as_message_segment(segment) for segment in filtered]), hits, True


def _node_identity(node: Any, self_id: str) -> Tuple[str, str]:
    """取合并转发第一个 node 的发送者信息，缺失时给出可用的兜底值"""
    data = _segment_data(node)
    name = data.get("name") or data.get("nickname") or "Bot"
    uin = data.get("uin") or data.get("user_id") or self_id
    return str(name), str(uin)


def _whole_message(settings: FilterSettings) -> Message:
    return Message(MessageSegment.text(settings.replacement))


def _apply_whole(api: str, data: Dict[str, Any], settings: FilterSettings, self_id: str) -> None:
    """按「整条替换」改写请求，供 whole 模式与引擎异常兜底共用"""
    if api in _MESSAGE_APIS:
        data["message"] = _whole_message(settings)
        return
    if api in _FORWARD_APIS:
        messages = data.get("messages")
        first = messages[0] if isinstance(messages, list) and messages else None
        name, uin = _node_identity(first, self_id)
        data["messages"] = [
            {
                "type": "node",
                "data": {
                    "name": name,
                    "uin": uin,
                    "content": _whole_message(settings),
                },
            },
        ]
        return
    if api in _AI_RECORD_APIS:
        data["text"] = settings.replacement


def _filter_payload(api: str, data: Dict[str, Any], settings: FilterSettings, self_id: str) -> Counter[str]:
    if api in _MESSAGE_APIS:
        message = data.get("message")
        auto_escape = bool(data.get("auto_escape"))
        if isinstance(message, str) and auto_escape:
            result = filter_text(message, settings.rules, settings.replacement)
            if result.hit:
                if settings.mode == "whole":
                    data["message"] = _whole_message(settings)
                elif result.text is not message:
                    data["message"] = result.text
            return result.hits

        filtered, hits, changed = _filter_message_value(message, settings, 0)
        if hits:
            if settings.mode == "whole":
                data["message"] = _whole_message(settings)
            elif changed:
                data["message"] = filtered
        return hits

    if api in _FORWARD_APIS:
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return Counter()
        filtered, hits, changed = _filter_segments(messages, settings, 0)
        if hits:
            if settings.mode == "whole":
                _apply_whole(api, data, settings, self_id)
            elif changed:
                data["messages"] = filtered
        return hits

    if api in _AI_RECORD_APIS:
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return Counter()
        result = filter_text(text, settings.rules, settings.replacement)
        if result.hit:
            if settings.mode == "whole":
                data["text"] = settings.replacement
            elif result.text is not text:
                data["text"] = result.text
        return result.hits

    return Counter()


def filter_onebot_payload(
    api: str,
    data: Dict[str, Any],
    settings: FilterSettings,
    self_id: str,
) -> Counter[str]:
    """原地过滤一次 OneBot 发送请求，返回命中计数

    纯函数：不碰 bot、不读配置、不写文件。引擎异常在这里按整条替换兜底，
    不让异常冒到 `call_api` 上（那会让这次发送直接失败）。
    """
    try:
        return _filter_payload(api, data, settings, self_id)
    except Exception:
        logger.exception("OneBot 输出过滤失败，按整条替换处理")
        try:
            _apply_whole(api, data, settings, self_id)
        except Exception:
            logger.exception("OneBot 输出过滤兜底也失败，保持原请求")
        return Counter({"error:engine": 1})


async def _filter_call(bot: BaseBot, api: str, data: Dict[str, Any]) -> None:
    if api not in FILTERED_APIS or not isinstance(bot, OneBotV11Bot):
        return
    if is_outbound_filtered():
        return

    instance_key = lookup_cached_instance_key(bot.self_id)
    if instance_key is None:
        try:
            instance = await DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, bot.self_id)
        except Exception as exc:  # noqa: BLE001 - 查不到按没有实例处理
            logger.warning(f"查询 OneBot 实例失败，按无实例处理: self_id={bot.self_id}, 错误: {exc}")
            instance = None
        instance_key = instance.instance_key if instance else None

    channel_id = channel_of_call(api, data)
    try:
        if instance_key and channel_id:
            settings = await settings_for_chat(build_chat_key(instance_key, channel_id))
        elif instance_key:
            settings = settings_for_instance(ADAPTER_KEY, instance_key)
        else:
            settings = settings_from_config(config)
    except Exception as exc:  # noqa: BLE001 - 读配置失败退回全局配置
        logger.warning(f"读取 OneBot 输出过滤配置失败，回退全局配置: 错误: {exc}")
        settings = settings_from_config(config)

    if settings is None:
        return

    hits = filter_onebot_payload(api, data, settings, bot.self_id)
    if hits:
        await record_hits_for_instance(ADAPTER_KEY, instance_key or "", hits)


async def _on_calling_api(bot: BaseBot, api: str, data: Dict[str, Any]) -> None:
    """`call_api` 钩子；任何异常都不能往外冒，否则本次发送会直接失败"""
    try:
        await _filter_call(bot, api, data)
    except Exception:
        logger.exception("OneBot 输出过滤钩子执行失败，已跳过本次过滤")


_installed = False


def install_output_filter_hook() -> None:
    """注册出站过滤钩子；重复调用不会注册两次"""
    global _installed
    if _installed:
        return
    _installed = True
    OneBotV11Bot.on_calling_api(_on_calling_api)


def _reset_installed_for_tests() -> None:
    """用例之间允许重新安装（钩子挂在类上，重复安装只会多跑一次）"""
    global _installed
    _installed = False


__all__ = [
    "FILTERED_APIS",
    "filter_onebot_payload",
    "install_output_filter_hook",
]
