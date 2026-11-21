"""发送路径上的输出清理：把卡片规则接到通用发送服务

## 为什么单独一层

`cleanup.py` 只负责「编译一条规则、按预算执行」，不知道频道、人设与宏上下文；发送服务
也不知道卡片表。这一层把两边接起来：按频道找到生效人设的扩展行，编译并缓存规则，再在
发送前把文字交给 `apply_compiled`。放在这里而不是发送服务里，是为了让发送服务只多一个
调用点，测试也能单独钉住查库、缓存与停用登记。

## 缓存

每条出站消息都查「频道 → 人设 → 扩展行」两次库代价不小，而频道生效人设与规则在几秒内
几乎不会变。这里缓存两层：

- `chat_key → (preset_id, 过期时刻)` 十秒，省掉频道与人设的两次查询；
- `(preset_id, card.update_time)` → 编译结果，规则真的改过（`update_time` 变了）就自然
  失效，不必等十秒。

两个缓存都有上限，满了丢最早一半，避免长期运行后按频道数无限增长。

## 停用太慢的规则

`regex` 的超时能掐断单次匹配，但一条每次都要超时的规则会让每条消息都白白花掉预算。
同一条（人设, 规则序号）累计超时三次就在本进程停用，并把停用登记暴露给扩展页；
管理员保存规则时 `forget_preset` 清掉登记，给这条规则一次重试的机会。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Optional

from nekro_agent.core.config import config as global_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset_card import DBPresetCard

from .cleanup import CompiledRule, apply_compiled, compile_rules
from .macros import MacroContext, expand_macros, sanitize_user_name

logger = get_sub_logger("tavern.cleanup_runtime")

# 编译缓存的上限。频道生效人设**不**缓存：换人设的入口很多（频道绑定、实例默认人设、
# 系统默认人设、删人设后的回退），挂失效钩子迟早漏一个，而换人设后的第一条消息恰恰是
# 开场白——用旧人设的规则去清理它最容易出错。每次发送按频道现查生效人设，规则本身按
# （人设, 扩展行更新时间）缓存编译结果。
MAX_CACHE_ENTRIES = 1024
# 一条规则累计超时这么多次就在本进程停用。
MAX_TIMEOUTS = 3

# 替换文字里出现这些记号才需要建宏上下文；建上下文要查一次最近发言人的昵称，
# 绝大多数规则（状态栏清理之类）用不到，不该为它们多查一次库。
_MACRO_MARK = re.compile(r"<(?:USER|BOT|CHAR)>", re.IGNORECASE)


@dataclass(frozen=True)
class RuleSet:
    """一个频道当前生效人设的清理规则

    `rule_names` 的键是规则在原列表里的序号，用来在停用日志里写明是哪一条；
    日志只写名字与序号，不带正文。
    """

    preset_id: int
    compiled: tuple[CompiledRule, ...]
    preset_name: str
    nickname: str
    rule_names: dict[int, str]


# (人设 ID, 扩展行更新时间) → 编译结果。
_compiled_cache: dict[tuple[int, Any], RuleSet] = {}
# (人设 ID, 规则序号) → 累计超时次数 / 停用时的规则名。
_timeouts: dict[tuple[int, int], int] = {}
_disabled: dict[tuple[int, int], str] = {}


def _drop_oldest_half(cache: dict[Any, Any]) -> None:
    """缓存满了丢最早写入的一半；dict 的插入顺序就是最早到最晚"""
    for key in list(cache)[: max(1, len(cache) // 2)]:
        cache.pop(key, None)


def _store(cache: dict[Any, Any], key: Any, value: Any) -> None:
    """写缓存并维持上限"""
    if len(cache) >= MAX_CACHE_ENTRIES:
        _drop_oldest_half(cache)
    cache[key] = value


def _needs_expand(replace_text: str) -> bool:
    """替换文字里有没有宏或旧式占位"""
    return "{{" in replace_text or _MACRO_MARK.search(replace_text) is not None


async def _latest_user_name(chat_key: str) -> str:
    """取这个频道最近一条非 Bot 消息的昵称，清洗后作为 `{{user}}`"""
    latest = (
        await DBChatMessage.filter(chat_key=chat_key)
        .exclude(sender_id="-1")
        .order_by("-id")
        .first()
    )
    nickname = str(latest.sender_nickname or "").strip() if latest is not None else ""
    return sanitize_user_name(nickname or None)


async def _resolve_preset(chat_key: str) -> Optional[tuple[int, str]]:
    """查频道的生效人设；频道不存在时返回 `None`，内置人设按 ID 0 返回"""
    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        return None
    preset = await channel.get_preset()
    preset_id = getattr(preset, "id", None)
    name = str(getattr(preset, "name", "") or "")
    if not isinstance(preset_id, int) or preset_id <= 0:
        return 0, name
    return preset_id, name


def _active_rules(rules: RuleSet) -> Optional[RuleSet]:
    """去掉本进程已停用的规则；一条都不剩时按没有规则处理"""
    if not _disabled:
        return rules
    active = tuple(rule for rule in rules.compiled if (rules.preset_id, rule.index) not in _disabled)
    if not active:
        return None
    if len(active) == len(rules.compiled):
        return rules
    return replace(rules, compiled=active)


async def rules_for_chat(chat_key: str) -> Optional[RuleSet]:
    """取这个频道当前生效人设的清理规则；不需要清理时返回 `None`

    闸关、频道不存在、没扩展行、没启用规则都返回 `None`，调用方据此一个字都不改。
    任何异常都吞掉并记 WARNING：清理是附加步骤，不能拦住发送。
    """
    # 回滚闸是全局项、不可覆盖；关着时一次表都不查。
    if not bool(global_config.TAVERN_COMPAT_ENABLED):
        return None

    try:
        resolved = await _resolve_preset(chat_key)
        if resolved is None:
            return None
        preset_id, preset_name = resolved

        # 内置默认人设没有行 ID，也就不可能有扩展行。
        if preset_id <= 0:
            return None

        card = await DBPresetCard.get_or_none(preset_id=preset_id)
        if card is None:
            return None
        raw_rules = card.cleanup_rules if isinstance(card.cleanup_rules, list) else []
        if not raw_rules:
            return None

        cache_key = (preset_id, card.update_time)
        base = _compiled_cache.get(cache_key)
        if base is None:
            compiled = compile_rules(raw_rules)
            if not compiled:
                return None
            names: dict[int, str] = {}
            for index in (rule.index for rule in compiled):
                raw = raw_rules[index] if 0 <= index < len(raw_rules) else {}
                name = raw.get("name") if isinstance(raw, dict) else ""
                names[index] = name if isinstance(name, str) else ""
            nickname = str(card.nickname or "") if card.nickname else ""
            base = RuleSet(
                preset_id=preset_id,
                compiled=tuple(compiled),
                preset_name=preset_name,
                nickname=nickname,
                rule_names=names,
            )
            _store(_compiled_cache, cache_key, base)

        return _active_rules(base)
    except Exception as error:  # noqa: BLE001 - 清理取规则失败不能让发送挂掉
        logger.warning(f"读取酒馆清理规则失败，本次不清理: chat_key={chat_key}, error={error!r}")
        return None


def _account_timeouts(rules: RuleSet, timed_out: list[int]) -> None:
    """累计超时次数，到三次就在本进程停用；日志不带正文"""
    for index in timed_out:
        key = (rules.preset_id, index)
        count = _timeouts.get(key, 0) + 1
        _timeouts[key] = count
        name = rules.rule_names.get(index, "")
        if count < MAX_TIMEOUTS or key in _disabled:
            logger.debug(f"清理规则超时: preset_id={rules.preset_id}, rule_index={index}, name={name}, count={count}")
            continue
        _disabled[key] = name
        logger.warning(
            f"清理规则累计超时 {count} 次，已在本进程停用: preset_id={rules.preset_id}, rule_index={index}, name={name}",
        )


async def apply_cleanup(chat_key: str, text: str, rules: RuleSet) -> str:
    """对一条出站文字执行清理；任何异常都返回原文

    只有替换文字里出现宏记号时才建宏上下文：`{{char}}` 用昵称或人设名，
    `{{user}}` 取频道最近一条非 Bot 消息的昵称（每条消息最多查一次）。
    """
    try:
        expand = _identity
        if any(_needs_expand(rule.replace) for rule in rules.compiled):
            macro = MacroContext(
                char_name=rules.nickname or rules.preset_name,
                user_name=await _latest_user_name(chat_key),
                now=datetime.now(),
                lang=str(global_config.SYSTEM_LANG),
                chat_key=chat_key,
                preset_id=rules.preset_id,
                text_kind="cleanup",
            )
            expand = lambda value: expand_macros(value, macro).text  # noqa: E731 - 只在这里用一次

        result = apply_compiled(
            text,
            rules.compiled,
            expand=expand,
            per_call_seconds=0.05,
            budget_seconds=0.1,
        )
        _account_timeouts(rules, result.timed_out)
        for index in result.skipped_by_budget:
            logger.debug(
                f"清理预算用完，跳过规则: preset_id={rules.preset_id}, rule_index={index}, "
                f"name={rules.rule_names.get(index, '')}",
            )
        return result.text
    except Exception as error:  # noqa: BLE001 - 发送不能因为清理挂掉
        logger.warning(
            f"酒馆输出清理失败，原文照发: chat_key={chat_key}, preset_id={rules.preset_id}, error={error!r}",
        )
        return text


def _identity(text: str) -> str:
    """没有宏上下文时的展开函数：原样返回"""
    return text


def disabled_cleanup_rules() -> dict[tuple[int, int], str]:
    """本进程已停用的清理规则 `(人设 ID, 规则序号) → 规则名`，给扩展页显示用"""
    return dict(_disabled)


def forget_preset(preset_id: int) -> None:
    """清掉某个人设在本进程的停用与超时登记

    管理员在扩展页保存规则时调用：改过的规则应该重新获得完整的超时额度，不能因为
    改之前的旧规则太慢就一直停用。
    """
    for key in [key for key in _disabled if key[0] == preset_id]:
        _disabled.pop(key, None)
    for key in [key for key in _timeouts if key[0] == preset_id]:
        _timeouts.pop(key, None)


__all__ = [
    "MAX_CACHE_ENTRIES",
    "RuleSet",
    "apply_cleanup",
    "disabled_cleanup_rules",
    "forget_preset",
    "rules_for_chat",
]
