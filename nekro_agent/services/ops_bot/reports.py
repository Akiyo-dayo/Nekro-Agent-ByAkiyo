"""运维只读指令的取数与排版

## 一律复用总后台的取数函数

`list_instances()` / `build_overview()` / `instance_health_service.list_health()`
就是总后台页面在用的同一批函数。运维 Bot 不另写查询，
否则"后台显示在线、Bot 说离线"这种最难排查的不一致迟早出现。

## 输出为什么要截断

IM 的单条消息有长度上限，超长会被平台截断或拒发。这里统一按条数截断并注明还有多少条，
比让平台随机截断成半截 JSON 要好。想看全量就去后台。
"""

from __future__ import annotations

from typing import List

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("ops_bot.reports")

# 单条 IM 消息里最多列出的条目数
MAX_LIST_ITEMS = 20
# 错误摘要里单行最多保留的字符数
MAX_ERROR_LINE_CHARS = 160


def _truncate_note(shown: int, total: int) -> List[str]:
    if total <= shown:
        return []
    return [f"…… 另有 {total - shown} 条，请在总后台查看"]


async def render_overview(target: str = "") -> str:  # noqa: ARG001 - 统一的渲染函数签名，本指令不使用参数
    """总览

    「未绑定账号」必须与「离线」分开列出，理由与总后台首页同一条：它们是两种不同的处置。
    只列离线还会让这份总览**漏掉**那一档——`build_overview()` 把未绑定实例从 offline 里摘走后，
    不补一列的话它们在运维播报里既不在线也不离线也不待认领，四个数加不回总数，
    而这正是「我建的实例一直不收消息」最先该被看见的地方。
    """
    from nekro_agent.services.console.instance_manager import build_overview

    overview = await build_overview()
    return "\n".join(
        [
            "[运维总览]",
            f"实例总数: {overview.instance_total}",
            f"在线: {overview.instance_online} / 离线: {overview.instance_offline} / "
            f"未绑定账号: {overview.instance_unbound} / 待认领: {overview.instance_unclaimed}",
            f"会话总数: {overview.channel_total}",
            f"今日消息: {overview.message_count_today}",
            f"Docker 可用: {'是' if overview.docker_available else '否'}",
        ],
    )


async def render_instances(target: str = "") -> str:  # noqa: ARG001 - 统一的渲染函数签名
    """实例列表与在线状态"""
    from nekro_agent.services.console.instance_manager import list_instances

    response = await list_instances()
    if not response.items:
        return "[实例列表]\n当前没有任何实例"

    lines = [f"[实例列表] 共 {response.total} 个"]
    for item in response.items[:MAX_LIST_ITEMS]:
        flag = "" if item.enabled else "（已停用）"
        account = item.provider_account_id or "未绑定"
        lines.append(f"- {item.instance_key} [{item.status.value}]{flag} 账号={account} 今日消息={item.stats.message_count_today}")
    lines.extend(_truncate_note(min(len(response.items), MAX_LIST_ITEMS), response.total))
    return "\n".join(lines)


async def render_health(target: str = "") -> str:  # noqa: ARG001 - 统一的渲染函数签名
    """探活结果与掉线时长"""
    from nekro_agent.services.console.instance_health import instance_health_service

    response = await instance_health_service.list_health()
    if not response.items:
        return "[实例健康]\n没有处于监测中的实例"

    lines = [f"[实例健康] 共 {response.total} 个，探活间隔 {response.check_interval_seconds}s"]
    for item in response.items[:MAX_LIST_ITEMS]:
        offline = f" 已离线 {item.offline_seconds}s" if item.offline_since else ""
        alert = " [已告警]" if item.alerted else ""
        error = f" 最近错误: {item.last_error}" if item.last_error else ""
        lines.append(f"- {item.instance_key} [{item.status}]{offline}{alert}{error}")
    lines.extend(_truncate_note(min(len(response.items), MAX_LIST_ITEMS), response.total))
    return "\n".join(lines)


async def render_tenants(target: str = "") -> str:  # noqa: ARG001 - 统一的渲染函数签名
    """租户概览"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_tenant import DBTenant
    from nekro_agent.models.db_user import DBUser

    tenants = await DBTenant.all().order_by("id")
    if not tenants:
        return "[租户概览]\n尚未创建任何租户（当前为单一平台方模式）"

    lines = [f"[租户概览] 共 {len(tenants)} 个"]
    for tenant in tenants[:MAX_LIST_ITEMS]:
        instance_count = await DBAdapterInstance.filter(tenant_id=tenant.id).count()
        member_count = await DBUser.filter(tenant_id=tenant.id).count()
        lines.append(
            f"- #{tenant.id} {tenant.name} [{tenant.status}] 实例={instance_count} 成员={member_count}",
        )
    lines.extend(_truncate_note(min(len(tenants), MAX_LIST_ITEMS), len(tenants)))
    return "\n".join(lines)


async def render_errors(target: str = "") -> str:  # noqa: ARG001 - 统一的渲染函数签名
    """近期错误摘要

    取的是内存日志环，与 WebUI 的日志页同源。只读 ERROR/CRITICAL，
    WARNING 在本项目里过于常见，混进来会把真正的错误淹掉。
    """
    from nekro_agent.core.logger import get_log_records

    records = await get_log_records(page=1, page_size=200)
    if not isinstance(records, list):
        return "[近期错误]\n日志读取异常"

    errors = [record for record in records if str(record.get("level", "")).upper() in ("ERROR", "CRITICAL")]
    if not errors:
        return "[近期错误]\n最近 200 条日志中没有错误"

    lines = [f"[近期错误] 最近 200 条日志中有 {len(errors)} 条错误"]
    for record in errors[:MAX_LIST_ITEMS]:
        message = str(record.get("message", "")).replace("\n", " ")[:MAX_ERROR_LINE_CHARS]
        lines.append(f"- {record.get('timestamp', '')} [{record.get('source', '')}] {message}")
    lines.extend(_truncate_note(min(len(errors), MAX_LIST_ITEMS), len(errors)))
    return "\n".join(lines)


async def render_audit(target: str = "") -> str:
    """最近的运维操作记录

    条数与 `ops_audit_recent` 同口径：默认 10、最多 20；行格式也共用
    `services/ops_agent/audit.py` 的格式化函数，两条通道看到的是同一套文字。
    """
    from nekro_agent.services.ops_agent.audit import (
        DEFAULT_RECENT_LINES,
        MAX_RECENT_LINES,
        format_ops_event_lines,
        list_ops_events,
    )

    text = str(target or "").strip()
    limit = int(text) if text.isdigit() else DEFAULT_RECENT_LINES
    limit = max(1, min(MAX_RECENT_LINES, limit))
    _, rows = await list_ops_events(limit=limit)
    return format_ops_event_lines(rows)


READ_RENDERERS = {
    "overview": render_overview,
    "instances": render_instances,
    "health": render_health,
    "tenants": render_tenants,
    "errors": render_errors,
    "audit": render_audit,
}
