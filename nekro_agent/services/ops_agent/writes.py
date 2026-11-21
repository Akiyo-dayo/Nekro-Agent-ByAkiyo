"""运维写操作的共用执行层

## 这里只做三件事

解析目标、调用总后台正在用的同一个业务函数、把结果收成一个 `WriteOutcome`。
**不发回执、不写审计、不判授权**：回执在 `receipt.py`，授权在 `round_grant.py`，
审计在 `audit.py`。分出这一层的理由是运维工具与 `/na_ops` 必须走**同一条**执行路径，
否则两条通道的副作用迟早会分叉（其中一条漏掉缓存失效、状态广播或将来新增的校验）。

## 失败类别为什么是固定短语

异常原文里可能有上游返回的用户对话、文件路径甚至凭据，而回执会发回聊天频道。
所以对外只给固定的几个类别，原文只进宿主日志（带异常类名），
见 `_log_failure`。目标不明确时额外带候选标识，让运维人员自己说清是哪一个。

## 实例目标必须成对

`instance_key` 只在同一适配器内唯一，同名实例跨适配器时按单个标识取第一个会操作错对象。
因此运维工具要求 `(adapter_key, instance_key)` 两个都给；`/na_ops` 只写实例标识时，
若未删除的同名实例不止一个就返回 `目标不明确`，绝不替人挑一个。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.console import InstanceProvider
from nekro_agent.schemas.tenant import TenantStatus
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.chat_channel_service import set_channel_active as apply_channel_active
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.console import instance_manager
from nekro_agent.services.console.container_manager import restart_container
from nekro_agent.services.console.tenant_manager import set_tenant_status as apply_tenant_status

from .text import ident, name

logger = get_sub_logger("ops_agent.writes")

# 失败类别：回执与工具返回值里只会出现这几个短语，绝不带上游原文
ERROR_NOT_FOUND = "目标不存在"
ERROR_AMBIGUOUS = "目标不明确"
ERROR_INVALID_ARGUMENT = "参数不合法"
ERROR_UNSUPPORTED_INSTANCE = "不支持该实例"
ERROR_EXECUTION = "执行出错"

CONFIG_KEY = "system"
TOOLS_ENABLED_KEY = "OPS_AGENT_TOOLS_ENABLED"

INSTANCE_ACTIONS = frozenset({"enable-instance", "disable-instance", "restart-container"})
TENANT_ACTIONS = frozenset({"suspend-tenant", "resume-tenant"})


@dataclass(frozen=True)
class WriteOutcome:
    """一次写操作的结果

    Attributes:
        ok: 业务函数是否成功执行
        action_label: 给人看的动作名，如「停用实例」
        target_label: 给人看的目标，名字过 `_name`、标识过 `_ident`
        error_category: 失败时的固定类别；成功时为空串
        candidates: 目标模糊时的候选标识（已过 `_ident`）
    """

    ok: bool
    action_label: str
    target_label: str
    error_category: str = ""
    candidates: Tuple[str, ...] = ()


def _ok(action_label: str, target_label: str) -> WriteOutcome:
    return WriteOutcome(ok=True, action_label=action_label, target_label=target_label)


def _fail(
    action_label: str,
    target_label: str,
    error_category: str,
    candidates: Tuple[str, ...] = (),
) -> WriteOutcome:
    return WriteOutcome(
        ok=False,
        action_label=action_label,
        target_label=target_label,
        error_category=error_category,
        candidates=candidates,
    )


def _log_failure(action_label: str, exc: Exception) -> None:
    """失败原文只进日志：回执与工具返回值里永远只有固定类别"""
    logger.warning(f"[ops_agent] 写操作执行失败: {action_label} | {type(exc).__name__}: {exc}")


def _instance_input_label(adapter_key: str, instance_key: str) -> str:
    adapter = str(adapter_key or "").strip()
    key = str(instance_key or "").strip()
    return f"{ident(adapter)}/{ident(key)}" if adapter else ident(key)


def _instance_label(instance: DBAdapterInstance) -> str:
    shown_name = instance.display_name or instance.instance_key
    return f"{ident(instance.adapter_key)}/{ident(instance.instance_key)}（{name(shown_name)}）"


def _tenant_label(tenant: DBTenant) -> str:
    return f"{tenant.id}（{name(tenant.display_name or tenant.name)}）"


def _channel_label(channel: DBChatChannel) -> str:
    shown_name = (channel.channel_name or "").strip()
    if shown_name:
        return f"{ident(channel.chat_key)}（{name(shown_name)}）"
    return ident(channel.chat_key)


async def _find_instance(adapter_key: str, instance_key: str) -> Optional[DBAdapterInstance]:
    """按两者精确查、排除软删墓碑"""
    adapter = str(adapter_key or "").strip()
    key = str(instance_key or "").strip()
    if not adapter or not key:
        return None
    return (
        await DBAdapterInstance.filter(adapter_key=adapter, instance_key=key)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
        .first()
    )


async def _resolve_instance_word(target: str) -> Tuple[Optional[DBAdapterInstance], str, Tuple[str, ...]]:
    """`/na_ops` 的单目标解析

    写成 `适配器标识:实例标识` 就按对查；只写实例标识时，若有多个未删除的同名实例，
    返回 `目标不明确` 与候选，由运维人员补全，不替人挑第一个。
    """
    text = str(target or "").strip()
    if not text:
        return None, ERROR_INVALID_ARGUMENT, ()
    adapter_key, separator, instance_key = text.partition(":")
    if separator:
        instance = await _find_instance(adapter_key, instance_key)
        return (instance, "", ()) if instance is not None else (None, ERROR_NOT_FOUND, ())

    rows = (
        await DBAdapterInstance.filter(instance_key=text)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
    )
    if not rows:
        return None, ERROR_NOT_FOUND, ()
    if len(rows) > 1:
        candidates = tuple(f"{ident(row.adapter_key)}:{ident(row.instance_key)}" for row in rows)
        return None, ERROR_AMBIGUOUS, candidates
    return rows[0], "", ()


def _parse_tenant_id(value: object) -> Optional[int]:
    """租户 ID：正整数或纯数字串，其余一律不认

    `bool` 是 `int` 的子类，`int(True)` 是 1：不单独排除的话，模型把参数写成 `True`
    就会停掉 1 号租户。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


async def _find_tenant(tenant_id: int) -> Optional[DBTenant]:
    return await DBTenant.get_or_none(id=tenant_id)


async def _find_channel(chat_key: str) -> Optional[DBChatChannel]:
    key = str(chat_key or "").strip()
    if not key:
        return None
    return await DBChatChannel.filter(chat_key=key).order_by("id").first()


def _valid_amount(amount: object) -> bool:
    """正整数；`bool` 是 `int` 的子类，必须单独排除"""
    return isinstance(amount, int) and not isinstance(amount, bool) and amount > 0


async def set_instance_enabled(adapter_key: str, instance_key: str, enabled: bool) -> WriteOutcome:
    """启用 / 停用实例

    `enabled` 只认真正的布尔值：`bool("false")` 是 True，模型把参数写成字符串时，
    宽松转换会把「停用」执行成「启用」。
    """
    target_label = _instance_input_label(adapter_key, instance_key)
    if not isinstance(enabled, bool):
        return _fail("启停实例", target_label, ERROR_INVALID_ARGUMENT)
    action_label = "启用实例" if enabled else "停用实例"
    try:
        instance = await _find_instance(adapter_key, instance_key)
        if instance is None:
            return _fail(action_label, target_label, ERROR_NOT_FOUND)
        target_label = _instance_label(instance)
        await instance_manager.set_instance_enabled(instance, enabled)
        return _ok(action_label, target_label)
    except Exception as exc:  # noqa: BLE001 - 任何异常都只能给出固定类别
        _log_failure(action_label, exc)
        return _fail(action_label, target_label, ERROR_EXECUTION)


async def restart_protocol(adapter_key: str, instance_key: str) -> WriteOutcome:
    """重启协议端；只支持内置 NapCat"""
    action_label = "重启协议端"
    target_label = _instance_input_label(adapter_key, instance_key)
    try:
        instance = await _find_instance(adapter_key, instance_key)
        if instance is None:
            return _fail(action_label, target_label, ERROR_NOT_FOUND)
        target_label = _instance_label(instance)
        if str(instance.provider or "").strip().lower() != InstanceProvider.BUILTIN_NAPCAT.value:
            return _fail(action_label, target_label, ERROR_UNSUPPORTED_INSTANCE)
        await restart_container(instance)
        return _ok(action_label, target_label)
    except Exception as exc:  # noqa: BLE001
        _log_failure(action_label, exc)
        return _fail(action_label, target_label, ERROR_EXECUTION)


async def set_tenant_status(tenant_id: int, status: str) -> WriteOutcome:
    """停用 / 恢复租户；只接受 active 与 suspended"""
    wanted = str(status or "").strip().lower()
    action_label = "恢复租户" if wanted == TenantStatus.ACTIVE.value else "停用租户"
    target_label = ident(tenant_id)
    parsed_id = _parse_tenant_id(tenant_id)
    if parsed_id is None or wanted not in (TenantStatus.ACTIVE.value, TenantStatus.SUSPENDED.value):
        return _fail(action_label, target_label, ERROR_INVALID_ARGUMENT)
    target_label = str(parsed_id)
    try:
        tenant = await _find_tenant(parsed_id)
        if tenant is None:
            return _fail(action_label, target_label, ERROR_NOT_FOUND)
        target_label = _tenant_label(tenant)
        await apply_tenant_status(tenant, wanted)
        return _ok(action_label, target_label)
    except Exception as exc:  # noqa: BLE001
        _log_failure(action_label, exc)
        return _fail(action_label, target_label, ERROR_EXECUTION)


async def set_channel_active(chat_key: str, active: bool) -> WriteOutcome:
    """启用 / 停用频道；`active` 只认真正的布尔值（理由同 `set_instance_enabled`）"""
    target_label = ident(chat_key)
    if not isinstance(active, bool):
        return _fail("启停频道", target_label, ERROR_INVALID_ARGUMENT)
    action_label = "启用频道" if active else "停用频道"
    try:
        channel = await _find_channel(chat_key)
        if channel is None:
            return _fail(action_label, target_label, ERROR_NOT_FOUND)
        target_label = _channel_label(channel)
        await apply_channel_active(channel, active)
        return _ok(action_label, target_label)
    except Exception as exc:  # noqa: BLE001
        _log_failure(action_label, exc)
        return _fail(action_label, target_label, ERROR_EXECUTION)


async def add_quota_bonus(chat_key: str, amount: int) -> WriteOutcome:
    """给频道今天加一笔临时额度；次日作废"""
    action_label = "频道加额"
    target_label = ident(chat_key)
    if not _valid_amount(amount):
        return _fail(action_label, target_label, ERROR_INVALID_ARGUMENT)
    try:
        channel = await _find_channel(chat_key)
        if channel is None:
            return _fail(action_label, target_label, ERROR_NOT_FOUND)
        target_label = _channel_label(channel)
        await counter.add_bonus(channel.chat_key, counter.quota_day(), int(amount))
        return _ok(action_label, target_label)
    except Exception as exc:  # noqa: BLE001
        _log_failure(action_label, exc)
        return _fail(action_label, target_label, ERROR_EXECUTION)


def _restore_tools_switch(previous: bool) -> None:
    """把活开关改回原值；回滚本身失败时只记日志，不能再抛"""
    try:
        UnifiedConfigService.set_config_value(CONFIG_KEY, TOOLS_ENABLED_KEY, "true" if previous else "false")
    except Exception as exc:  # noqa: BLE001 - 回滚失败只影响下一次读取，不能盖住原始错误
        logger.warning(f"[ops_agent] 回滚运维工具开关失败: {type(exc).__name__}")


async def set_tools_enabled(enabled: bool) -> WriteOutcome:
    """写运维工具总开关并落盘

    `set_config_value` 会就地改活对象，所以落盘失败时把它改回去：
    回执说「未执行」而开关其实已经关掉，是比失败本身更难排查的状态。
    """
    action_label = "开启运维工具" if enabled else "关闭运维工具"
    target_label = "运维工具总开关"
    # 回滚用写入前**读到的**值，不能由目标值反推：本来就关着时执行「关」、落盘又失败，
    # 反推出来的「原值」是开，回滚等于把工具打开了。函数内导入，理由同 `round_grant`
    from nekro_agent.core.config import config as core_config

    previous = bool(getattr(core_config, TOOLS_ENABLED_KEY, False))
    changed = False
    try:
        success, message = UnifiedConfigService.set_config_value(
            CONFIG_KEY,
            TOOLS_ENABLED_KEY,
            "true" if enabled else "false",
        )
        if not success:
            logger.warning(f"[ops_agent] 写运维工具开关失败: {message}")
            return _fail(action_label, target_label, ERROR_EXECUTION)
        changed = True
        saved, save_message = UnifiedConfigService.save_config(CONFIG_KEY)
        if not saved:
            logger.warning(f"[ops_agent] 运维工具开关落盘失败: {save_message}")
            _restore_tools_switch(previous)
            return _fail(action_label, target_label, ERROR_EXECUTION)
        return _ok(action_label, target_label)
    except Exception as exc:  # noqa: BLE001
        _log_failure(action_label, exc)
        if changed:
            _restore_tools_switch(previous)
        return _fail(action_label, target_label, ERROR_EXECUTION)


async def execute_na_ops_write(action: str, target: str) -> WriteOutcome:
    """`/na_ops` 的写操作入口

    与运维工具共用下面这些函数；唯一的差别是实例目标是一个词，需要先解析成
    `(adapter_key, instance_key)` 这一对。
    """
    if action in INSTANCE_ACTIONS:
        action_label = {
            "enable-instance": "启用实例",
            "disable-instance": "停用实例",
            "restart-container": "重启协议端",
        }[action]
        input_label = ident(target)
        try:
            instance, error, candidates = await _resolve_instance_word(target)
        except Exception as exc:  # noqa: BLE001
            _log_failure(action_label, exc)
            return _fail(action_label, input_label, ERROR_EXECUTION)
        if instance is None:
            return _fail(action_label, input_label, error or ERROR_NOT_FOUND, candidates)
        if action == "restart-container":
            return await restart_protocol(instance.adapter_key, instance.instance_key)
        return await set_instance_enabled(instance.adapter_key, instance.instance_key, action == "enable-instance")

    if action in TENANT_ACTIONS:
        action_label = "停用租户" if action == "suspend-tenant" else "恢复租户"
        text = str(target or "").strip()
        if not text.isdigit():
            return _fail(action_label, ident(text), ERROR_INVALID_ARGUMENT)
        status = TenantStatus.SUSPENDED.value if action == "suspend-tenant" else TenantStatus.ACTIVE.value
        return await set_tenant_status(int(text), status)

    if action == "tools":
        wanted = str(target or "").strip().lower()
        if wanted not in ("on", "off"):
            return _fail("运维工具开关", ident(target), ERROR_INVALID_ARGUMENT)
        return await set_tools_enabled(wanted == "on")

    return _fail("运维写操作", ident(target), ERROR_INVALID_ARGUMENT)


__all__ = [
    "ERROR_AMBIGUOUS",
    "ERROR_EXECUTION",
    "ERROR_INVALID_ARGUMENT",
    "ERROR_NOT_FOUND",
    "ERROR_UNSUPPORTED_INSTANCE",
    "WriteOutcome",
    "add_quota_bonus",
    "execute_na_ops_write",
    "restart_protocol",
    "set_channel_active",
    "set_instance_enabled",
    "set_tenant_status",
    "set_tools_enabled",
]
