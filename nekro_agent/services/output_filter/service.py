"""对外入口：从配置拿到过滤设置，并把发送链路接上

这一层负责「配置 -> 设置」与「设置 -> 发送段」的转换，引擎与规则解析保持纯函数。
防重复的上下文也在这里：第 1 层过滤过的出站，第 2 层的 OneBot 钩子不能再过滤一次。
"""

import contextlib
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, Optional

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.core.config import CoreConfig
from nekro_agent.core.config import config as system_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.config_resolver import config_resolver

from .engine import TextFilterResult, filter_text
from .rules import CompiledRules, compile_rules
from .stats import record_hits_for_chat

logger = get_sub_logger("output_filter")

DEFAULT_REPLACEMENT = "**Filter**"
STARTER_WORDS_PATH = Path(__file__).parent / "starter_words.txt"


@dataclass(frozen=True)
class FilterSettings:
    mode: Literal["fragment", "whole"]
    replacement: str
    rules: CompiledRules


def settings_from_config(cfg: CoreConfig) -> Optional[FilterSettings]:
    """从一份配置里解析出过滤设置；关闭或没有任何可用规则时返回 None"""
    if not cfg.OUTPUT_FILTER_ENABLE:
        return None
    rules = compile_rules(cfg.OUTPUT_FILTER_WORDS, cfg.OUTPUT_FILTER_REGEX)
    if rules.empty:
        return None
    replacement = cfg.OUTPUT_FILTER_REPLACEMENT.strip() or DEFAULT_REPLACEMENT
    return FilterSettings(mode=cfg.OUTPUT_FILTER_MODE, replacement=replacement, rules=rules)


async def settings_for_chat(chat_key: str) -> Optional[FilterSettings]:
    """频道视角的生效设置；解析失败时退回全局配置，出站不能因为读配置失败而停摆"""
    try:
        effective = await config_resolver.get_effective_config(chat_key)
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都按「读不到」处理
        logger.warning(f"读取频道输出过滤配置失败，回退全局配置: chat_key={chat_key}, 错误: {exc}")
        effective = system_config
    return settings_from_config(effective)


def settings_for_instance(adapter_key: str, instance_key: str) -> Optional[FilterSettings]:
    """实例视角的生效设置（实例 > 适配器 > 全局）"""
    try:
        effective = config_resolver.get_instance_effective_config(adapter_key, instance_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"读取实例输出过滤配置失败，回退全局配置: {adapter_key}/{instance_key}, 错误: {exc}",
        )
        effective = system_config
    return settings_from_config(effective)


_outbound_filtered: ContextVar[bool] = ContextVar("output_filter_outbound_filtered", default=False)


def is_outbound_filtered() -> bool:
    """当前出站是否已经在第 1 层过滤过"""
    return _outbound_filtered.get()


@contextlib.contextmanager
def outbound_filtered() -> Iterator[None]:
    """标记「本次出站已经过第 1 层过滤」，供 OneBot 的 call_api 钩子跳过"""
    token = _outbound_filtered.set(True)
    try:
        yield
    finally:
        _outbound_filtered.reset(token)


async def filter_send_segments(
    chat_key: str,
    segments: list[PlatformSendSegment],
) -> tuple[list[PlatformSendSegment], Optional[FilterSettings], Counter[str]]:
    """过滤一组待发送的消息段

    Returns:
        (过滤后的消息段, 生效设置, 命中计数)。没有任何命中时第一个返回值就是入参那个 list，
        调用方可以用 `is` 判断是否需要重建请求。
    """
    settings = await settings_for_chat(chat_key)
    if settings is None:
        return segments, None, Counter()

    results: dict[int, TextFilterResult] = {}
    hits: Counter[str] = Counter()
    try:
        for index, segment in enumerate(segments):
            if segment.type != PlatformSendSegmentType.TEXT or not segment.content:
                continue
            result = filter_text(
                segment.content,
                settings.rules,
                settings.replacement,
                protect_at_markup=True,
            )
            results[index] = result
            hits.update(result.hits)
    except Exception:
        # 过滤引擎本身不该抛异常；真抛了也不能把原文发出去，按 whole 处理（fail-closed）
        logger.exception("输出过滤引擎执行失败，按整条替换处理")
        error_hits: Counter[str] = Counter({"error:engine": 1})
        filtered = [PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=settings.replacement)]
        await record_hits_for_chat(chat_key, error_hits)
        return filtered, settings, error_hits

    if not hits:
        return segments, settings, hits

    if settings.mode == "whole":
        filtered = [PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=settings.replacement)]
        await record_hits_for_chat(chat_key, hits)
        return filtered, settings, hits

    filtered_segments: list[PlatformSendSegment] = []
    for index, segment in enumerate(segments):
        result = results.get(index)
        if result is not None and result.hit:
            filtered_segments.append(segment.model_copy(update={"content": result.text}))
        else:
            filtered_segments.append(segment)
    await record_hits_for_chat(chat_key, hits)
    return filtered_segments, settings, hits


async def filter_platform_request(request: PlatformSendRequest) -> PlatformSendRequest:
    """过滤一个协议端发送请求；内容没变时返回原 request"""
    filtered, _settings, _hits = await filter_send_segments(request.chat_key, request.segments)
    if filtered is request.segments:
        return request
    return request.model_copy(update={"segments": filtered})


def _memory_filter_settings(cfg: CoreConfig) -> Optional[FilterSettings]:
    """记忆前过滤的两道开关：输出过滤本身开着，且 `OUTPUT_FILTER_BEFORE_MEMORY` 打开

    与 L2 回滚闸无关：它是输出过滤的功能，闸关时写工作区行照样生效。
    """
    if not getattr(cfg, "OUTPUT_FILTER_ENABLE", False) or not getattr(cfg, "OUTPUT_FILTER_BEFORE_MEMORY", False):
        return None
    return settings_from_config(cfg)


def _memory_replacement(cfg: CoreConfig) -> str:
    return (getattr(cfg, "OUTPUT_FILTER_REPLACEMENT", "") or "").strip() or DEFAULT_REPLACEMENT


def _apply_memory_filter(cfg: CoreConfig, text: str) -> tuple[str, bool]:
    """对一段将要写进长期记忆的文字套过滤，返回 (过滤后文字, 是否命中)

    开关关着或没有规则时原样返回；编译 / 引擎抛异常时 fail-closed：文字整段换成替换词，
    命中返回 True。只记 debug 的规则键与条数，不把原文写进日志。
    """
    if not getattr(cfg, "OUTPUT_FILTER_ENABLE", False) or not getattr(cfg, "OUTPUT_FILTER_BEFORE_MEMORY", False):
        return text, False
    replacement = _memory_replacement(cfg)
    try:
        settings = _memory_filter_settings(cfg)
        if settings is None:
            return text, False
        result = filter_text(text, settings.rules, settings.replacement)
    except Exception:
        logger.exception("记忆前过滤执行失败，按整条替换处理")
        return replacement, True
    if result.hits:
        logger.debug(f"记忆前过滤命中: rules={dict(result.hits)}, count={sum(result.hits.values())}")
    return result.text, result.hit


def filter_text_for_memory(cfg: CoreConfig, text: str) -> str:
    """长期记忆沉淀写入前的过滤：段落正文与摘要用这一出入口

    命中片段换成替换文字；开关关着或规则为空时逐字返回入参。
    """
    return _apply_memory_filter(cfg, text)[0]


def memory_filter_hit(cfg: CoreConfig, text: str) -> bool:
    """同一套规则的是否命中判据，供实体 / 关系这类整条丢弃的写入使用

    与 `filter_text_for_memory` 共用开关、规则与替换文字，避免两处判据漂移。
    """
    return _apply_memory_filter(cfg, text)[1]
