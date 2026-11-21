"""运维工具的实现

## 一律复用总后台的取数函数

`instance_manager.build_overview()` / `list_instances()` / `describe_instance()`、
`instance_health_service.list_health()`、`services/usage/query.py` 的聚合、
`services/channel_quota` 的 `evaluate` 就是总后台页面在用的同一批函数。运维工具不另写查询，
否则「后台显示在线、Bot 说离线」这种最难排查的不一致迟早出现。

写操作同理：全部落到 `writes.py`，由它调用总后台正在用的业务函数。

## 返回值为什么要清洗

结果会进模型上下文。里面出现的频道名、实例显示名、租户显示名都可能由租户自己填，
所以一律经 `tools/untrusted_content.py: echo_inline` 压成单行、剥掉不可见字符并加引号定界，
再截到 32 字。`ops_recent_errors` 的正文里可能带用户原文与上游报错，因此整体作为
`UntrustedToolResult` 返回——读过它的这一轮会被标记 `tainted`，写操作随即被拒。

## 写工具执行后一定发回执

成功或失败都发：回执是「直接执行、不二次确认」这道设计的可见性。被授权复核拒绝的调用
没有执行，因此也不发。
"""

from __future__ import annotations

from typing import Any, Dict

from tortoise.timezone import now as db_now

from nekro_agent.core.logger import get_log_records
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services import channel_quota
from nekro_agent.services.console.instance_health import instance_health_service
from nekro_agent.services.console.instance_manager import build_overview, describe_instance, list_instances
from nekro_agent.services.usage.query import UsageScope, aggregate_by, aggregate_totals
from nekro_agent.tools.untrusted_content import UntrustedToolResult, sanitize_block_text

from . import writes
from .audit import DEFAULT_RECENT_LINES, MAX_RECENT_LINES, current_ops_event, format_ops_event_lines, list_ops_events
from .receipt import format_receipt, send_receipt
from .round_grant import OpsRoundGrant
from .round_grant import get as get_grant
from .text import MAX_IDENT_CHARS, MAX_NAME_CHARS
from .text import ident as _ident
from .text import name as _name

# 列表类结果的条数上限：一条 IM 消息塞不下更多，也不该让工具结果挤占上下文
MAX_LIST_ITEMS = 50
MAX_FIND_ITEMS = 10
MAX_KEYWORD_CHARS = 64
MAX_ERROR_LINES = 50
MAX_ERROR_LINE_CHARS = 160
MAX_AUDIT_LINES = MAX_RECENT_LINES

UNAVAILABLE = "运维工具不可用：本轮没有运维授权，或授权已失效。"


def _grant_available(ctx: AgentCtx) -> bool:
    """工具入口的授权复查"""
    return bool(ctx.container_key) and get_grant(ctx.container_key) is not None


def _write_grant(ctx: AgentCtx) -> OpsRoundGrant | None:
    """写工具的授权取用：拿不到就不执行"""
    if not ctx.container_key:
        return None
    return get_grant(ctx.container_key)


async def _finish_write(ctx: AgentCtx, grant: OpsRoundGrant, outcome: writes.WriteOutcome) -> str:
    """发回执并把同一段文字（去掉前缀）交回模型

    回执正文就是模型该看到的内容：它不需要自己复述一遍执行结果，也不该在回执之外
    再补一句「已经处理好了」。目标模糊时额外提示问清楚，这是「没说清目标」而不是
    「对已说清的指令再确认一次」。

    审计槽存在时把结果填进去、把记录号写进回执：`rpc_exec` 随后用槽关闭审计行。
    没有槽（直接调工具函数的用例）就保持旧格式，不带记录号。
    """
    slot = current_ops_event.get()
    event_id = slot.event_id if slot is not None else None
    receipt = format_receipt(outcome, grant.operators, record_id=event_id)
    await send_receipt(ctx.from_chat_key or grant.chat_key, receipt)
    reply = receipt.removeprefix("【运维】")
    if slot is not None:
        slot.outcome = "executed" if outcome.ok else "failed"
        slot.reason = "" if outcome.ok else outcome.error_category
        slot.summary = reply
    if outcome.error_category == writes.ERROR_AMBIGUOUS and outcome.candidates:
        reply = f"{reply} 请向运维人员确认是哪一个。"
    return reply


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _today_window() -> tuple[Any, Any]:
    """今日的 `[00:00, 现在+1秒)`，与总后台首页同一口径"""
    start = db_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, db_now().replace(microsecond=0)


async def _adapter_key_of(instance_key: str) -> str:
    """实例标识 -> 适配器标识

    `UsageScope` 的实例过滤是 `(adapter_key, instance_key)` 两列，只给实例标识时
    必须先补上适配器，否则会按空适配器过滤出一行不剩。同名实例跨适配器时取最早那行，
    与 `DBAdapterInstance` 其它查询的取舍一致。
    """
    if not instance_key:
        return ""
    row = await DBAdapterInstance.filter(instance_key=instance_key).order_by("id").first()
    return str(row.adapter_key) if row is not None else ""


async def ops_overview(ctx: AgentCtx) -> str:
    """查看总览：实例总数与在线 / 离线 / 未绑定 / 待认领、会话数、今日消息与今日 token 用量

    想知道「现在整体怎么样」时先用它。
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    overview = await build_overview()
    start, end = _today_window()
    totals = await aggregate_totals(
        UsageScope(
            tenant_id=None,
            instance_key="",
            adapter_key="",
            start_time=start,
            end_time=end,
            is_platform_owner=True,
        ),
    )
    return "\n".join(
        [
            "[运维总览]",
            f"实例总数: {overview.instance_total}",
            f"在线: {overview.instance_online} / 离线: {overview.instance_offline} / "
            f"未绑定账号: {overview.instance_unbound} / 待认领: {overview.instance_unclaimed}",
            f"会话总数: {overview.channel_total}",
            f"今日消息: {overview.message_count_today}",
            f"Docker 可用: {'是' if overview.docker_available else '否'}",
            f"今日调用: {totals['records']} 次 / token {totals['token_total']} / 费用 {totals['cost']:.4f}",
        ],
    )


async def ops_list_instances(ctx: AgentCtx, status: str = "") -> str:
    """列出实例及其在线状态，可按状态过滤

    Args:
        status: 只列这个状态的实例（online / offline / unbound / unclaimed / error 等）；留空列全部
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    wanted = str(status or "").strip().lower()
    response = await list_instances()
    items = [item for item in response.items if not wanted or item.status.value == wanted]
    if not items:
        suffix = f"（过滤条件: {_name(wanted)}）" if wanted else ""
        return f"[实例列表] 没有符合条件的实例{suffix}"

    lines = [f"[实例列表] 共 {len(items)} 个" + (f"（全部 {response.total} 个）" if wanted else "")]
    for item in items[:MAX_LIST_ITEMS]:
        flag = "" if item.enabled else "（已停用）"
        account = item.provider_account_id or "未绑定"
        tenant = "平台方" if item.tenant_id is None else f"租户 {item.tenant_id}"
        lines.append(
            f"- {_ident(item.adapter_key)}/{_ident(item.instance_key)} [{item.status.value}]{flag} "
            f"名称={_name(item.display_name)} 归属={tenant} 账号={_name(account)} "
            f"今日消息={item.stats.message_count_today}",
        )
    if len(items) > MAX_LIST_ITEMS:
        lines.append(f"…… 另有 {len(items) - MAX_LIST_ITEMS} 个，请在总后台查看")
    return "\n".join(lines)


async def ops_instance_status(ctx: AgentCtx, instance_key: str, adapter_key: str = "") -> Any:
    """查看单个实例的详细状态、健康快照与最近错误

    有最近错误时结果按不可信内容返回：错误文本是上游与协议端给的原文，与 `ops_recent_errors`
    同一类，读过之后本轮后续的写操作会被拒绝。没有最近错误时仍是普通文本。

    Args:
        instance_key: 实例标识
        adapter_key: 适配器标识；同名实例存在于多个适配器下时必填，否则会返回候选
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    key = str(instance_key or "").strip()
    if not key:
        return "请给出实例标识（可用 ops_find 按名称查找）。"
    adapter = str(adapter_key or "").strip()

    rows = await DBAdapterInstance.filter(instance_key=key).order_by("id")
    if not rows:
        return f"找不到实例 {_ident(key)}。可以用 ops_find 按名称或标识查找。"
    if adapter:
        rows = [row for row in rows if row.adapter_key == adapter]
        if not rows:
            return f"适配器 {_ident(adapter)} 下没有实例 {_ident(key)}。"
    if len(rows) > 1:
        candidates = "、".join(_ident(f"{row.adapter_key}/{row.instance_key}") for row in rows)
        return f"实例标识 {_ident(key)} 在多个适配器下都存在：{candidates}。请带上 adapter_key 再查。"

    instance = rows[0]
    info = await describe_instance(instance)
    health = await instance_health_service.list_health()
    snapshot = next(
        (item for item in health.items if item.adapter_key == instance.adapter_key and item.instance_key == instance.instance_key),
        None,
    )

    lines = [
        f"[实例状态] {_ident(instance.adapter_key)}/{_ident(instance.instance_key)}",
        f"名称: {_name(info.display_name)}",
        f"状态: {info.status.value}（{'启用' if info.enabled else '已停用'}）",
        f"归属: {'平台方' if info.tenant_id is None else f'租户 {info.tenant_id}'}",
        f"账号: {_name(info.provider_account_id or '未绑定')}",
        f"今日消息: {info.stats.message_count_today}",
    ]
    if info.offline_since:
        lines.append(f"掉线起始: {_name(info.offline_since)}")
    if snapshot is not None:
        lines.append(
            f"健康: {_name(snapshot.status)} 已离线 {snapshot.offline_seconds}s "
            f"{'（已告警）' if snapshot.alerted else ''}",
        )
    if info.last_error:
        last_error = str(info.last_error).replace("\n", " ")[:MAX_ERROR_LINE_CHARS]
        # 摘要在不可信块之外、当可信文字用：只放上面那几行宿主拼的状态（名字与标识都已回显过），
        # 错误原文只进正文
        summary = "\n".join([*lines, "（最近错误见下面的外部内容块）"])
        return UntrustedToolResult(
            kind="OpsInstanceError",
            body=sanitize_block_text(f"最近错误: {last_error}", MAX_ERROR_LINE_CHARS + 16),
            summary=summary,
        )
    return "\n".join(lines)


async def ops_list_tenants(ctx: AgentCtx) -> str:
    """列出所有租户、状态与名下实例数"""
    if not _grant_available(ctx):
        return UNAVAILABLE

    tenants = await DBTenant.all().order_by("id")
    if not tenants:
        return "[租户列表] 尚未创建任何租户（当前为单一平台方模式）"

    lines = [f"[租户列表] 共 {len(tenants)} 个"]
    for tenant in tenants[:MAX_LIST_ITEMS]:
        instance_count = await DBAdapterInstance.filter(tenant_id=tenant.id).count()
        lines.append(
            f"- {tenant.id} 标识={_name(tenant.name)} 名称={_name(tenant.display_name or tenant.name)} "
            f"[{_name(tenant.status)}] 实例={instance_count}",
        )
    if len(tenants) > MAX_LIST_ITEMS:
        lines.append(f"…… 另有 {len(tenants) - MAX_LIST_ITEMS} 个，请在总后台查看")
    return "\n".join(lines)


async def ops_find(ctx: AgentCtx, keyword: str) -> str:
    """按关键词查找实例、租户与频道，返回精确标识

    目标不明确时先用它：实例标识与频道会话标识都可以直接拿去调别的工具。

    Args:
        keyword: 关键词，匹配实例标识 / 显示名、租户标识 / 显示名、频道会话标识 / 频道名
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    text = str(keyword or "").strip()[:MAX_KEYWORD_CHARS]
    if not text:
        return "请给出要查找的关键词。"

    instances = await DBAdapterInstance.filter(instance_key__icontains=text).order_by("id").limit(MAX_FIND_ITEMS)
    by_name = await DBAdapterInstance.filter(display_name__icontains=text).order_by("id").limit(MAX_FIND_ITEMS)
    seen_ids = {row.id for row in instances}
    instances.extend([row for row in by_name if row.id not in seen_ids])

    tenants = await DBTenant.filter(name__icontains=text).order_by("id").limit(MAX_FIND_ITEMS)
    tenant_by_display = await DBTenant.filter(display_name__icontains=text).order_by("id").limit(MAX_FIND_ITEMS)
    tenant_ids = {tenant.id for tenant in tenants}
    tenants.extend([tenant for tenant in tenant_by_display if tenant.id not in tenant_ids])
    if text.isdigit():
        tenant = await DBTenant.filter(id=int(text)).first()
        if tenant is not None and tenant.id not in {item.id for item in tenants}:
            tenants.insert(0, tenant)

    channels = await DBChatChannel.filter(chat_key__icontains=text).order_by("id").limit(MAX_FIND_ITEMS)
    channel_by_name = await DBChatChannel.filter(channel_name__icontains=text).order_by("id").limit(MAX_FIND_ITEMS)
    channel_ids = {channel.id for channel in channels}
    channels.extend([channel for channel in channel_by_name if channel.id not in channel_ids])

    if not instances and not tenants and not channels:
        return f"没有找到与 {_name(text)} 匹配的实例、租户或频道。"

    lines = [f"[查找结果] 关键词 {_name(text)}"]
    if instances:
        lines.append("实例:")
        for row in instances[:MAX_FIND_ITEMS]:
            lines.append(
                f"- {_ident(row.adapter_key)}/{_ident(row.instance_key)} 名称={_name(row.display_name)} "
                f"[{_name(row.status)}]",
            )
    if tenants:
        lines.append("租户:")
        for tenant in tenants[:MAX_FIND_ITEMS]:
            lines.append(
                f"- {tenant.id} 标识={_name(tenant.name)} 名称={_name(tenant.display_name or tenant.name)} "
                f"[{_name(tenant.status)}]",
            )
    if channels:
        lines.append("频道:")
        for channel in channels[:MAX_FIND_ITEMS]:
            lines.append(
                f"- {_ident(channel.chat_key)} 名称={_name(channel.channel_name or '')} "
                f"类型={_name(channel.channel_type or '')}",
            )
    return "\n".join(lines)


async def ops_recent_errors(ctx: AgentCtx, limit: int = 20) -> Any:
    """查看最近的错误日志（ERROR / CRITICAL）

    错误文本里可能带用户原文与上游报错，所以结果按不可信内容返回；读过它之后，
    本轮后续的写操作会被拒绝。

    Args:
        limit: 最多列出多少条，1 到 50，默认 20
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    count = _clamp(limit, 1, MAX_ERROR_LINES)
    records = await get_log_records(page=1, page_size=200)
    if not isinstance(records, list):
        return "[近期错误] 日志读取异常"

    errors = [record for record in records if str(record.get("level", "")).upper() in ("ERROR", "CRITICAL")]
    if not errors:
        return "[近期错误] 最近 200 条日志中没有错误"

    lines = [f"最近 200 条日志中有 {len(errors)} 条错误，这里列出前 {min(count, len(errors))} 条："]
    for record in errors[:count]:
        message = str(record.get("message", "")).replace("\n", " ")[:MAX_ERROR_LINE_CHARS]
        lines.append(f"- {record.get('timestamp', '')} [{record.get('source', '')}] {message}")
    body = sanitize_block_text("\n".join(lines), MAX_ERROR_LINES * MAX_ERROR_LINE_CHARS)
    return UntrustedToolResult(kind="OpsErrors", body=body, summary="近期错误日志摘要（来自宿主日志环）")


async def ops_usage_today(ctx: AgentCtx, tenant_id: int = 0, instance_key: str = "") -> str:
    """查看今日 token 与费用汇总，以及按实例分组的前 10 名

    Args:
        tenant_id: 只看这个租户，0 表示全部（平台方视角）
        instance_key: 只看这个实例，留空表示全部
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    key = str(instance_key or "").strip()
    adapter_key = await _adapter_key_of(key) if key else ""
    if key and not adapter_key:
        return f"找不到实例 {_ident(key)}。"
    start, end = _today_window()
    scope = UsageScope(
        tenant_id=int(tenant_id) or None,
        instance_key=key,
        adapter_key=adapter_key,
        start_time=start,
        end_time=end,
        is_platform_owner=True,
    )
    totals = await aggregate_totals(scope)
    groups = await aggregate_by(scope, "instance_key", limit=10)

    lines = [
        "[今日用量]",
        f"调用: {totals['records']} 次",
        f"token: 输入 {totals['token_input']} / 输出 {totals['token_output']} / 合计 {totals['token_total']}",
        f"费用: {totals['cost']:.4f}",
    ]
    if groups:
        lines.append("按实例:")
        for group in groups:
            lines.append(
                f"- {_ident(group['key'] or '未归属')} 调用={group['records']} token={group['token_total']} "
                f"费用={group['cost']:.4f}",
            )
    return "\n".join(lines)


async def ops_channel_quota(ctx: AgentCtx, chat_key: str) -> str:
    """查看某个频道的每日额度状态：用量、额度、是否暂停

    Args:
        chat_key: 完整会话标识，可用 ops_find 查到
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    key = str(chat_key or "").strip()
    if not key:
        return "请给出频道会话标识（可用 ops_find 查到）。"
    channel = await DBChatChannel.get_or_none(chat_key=key)
    if channel is None:
        return f"找不到频道 {_ident(key)}。"
    status = await channel_quota.evaluate(channel)
    return "\n".join(
        [
            f"[频道额度] {_ident(channel.chat_key)}",
            f"名称: {_name(channel.channel_name or '')}",
            f"状态: {_name(status.state)}",
            f"单位: {_name(status.unit)}",
            f"已用: {status.used} / 额度: {status.allowed or '不限'}（加额 {status.bonus}）",
            f"占比: {status.percent}%",
            f"超额动作: {_name(status.action)}",
        ],
    )


async def ops_audit_recent(ctx: AgentCtx, limit: int = 10) -> str:
    """查看最近的运维操作记录（谁在什么地点执行或拒绝了什么）

    结果里不含调用参数与异常类名：参数是模型自己写的、可能夹带外部内容，这一行要能
    当作可信结果交回模型。读过它不会让本轮变成「读过外部内容」。

    Args:
        limit: 最多列出多少条，1 到 20，默认 10
    """
    if not _grant_available(ctx):
        return UNAVAILABLE

    if isinstance(limit, bool) or not isinstance(limit, int):
        count = DEFAULT_RECENT_LINES
    else:
        count = _clamp(limit, 1, MAX_AUDIT_LINES)
    _, rows = await list_ops_events(limit=count)
    return format_ops_event_lines(rows)


async def ops_set_instance_enabled(ctx: AgentCtx, adapter_key: str, instance_key: str, enabled: bool) -> str:
    """启用或停用某个实例，使其恢复 / 停止收发消息

    Args:
        adapter_key: 适配器标识
        instance_key: 实例标识
        enabled: True 启用、False 停用

    会直接生效，执行后宿主会在本频道发一条回执。
    """
    grant = _write_grant(ctx)
    if grant is None:
        return UNAVAILABLE
    outcome = await writes.set_instance_enabled(adapter_key, instance_key, enabled)
    return await _finish_write(ctx, grant, outcome)


async def ops_restart_protocol(ctx: AgentCtx, adapter_key: str, instance_key: str) -> str:
    """重启某个实例的协议端容器，只支持平台方内置的 NapCat

    Args:
        adapter_key: 适配器标识
        instance_key: 实例标识

    会直接生效，执行后宿主会在本频道发一条回执。远程协议端或不托管的适配器会被拒绝。
    """
    grant = _write_grant(ctx)
    if grant is None:
        return UNAVAILABLE
    outcome = await writes.restart_protocol(adapter_key, instance_key)
    return await _finish_write(ctx, grant, outcome)


async def ops_set_tenant_status(ctx: AgentCtx, tenant_id: int, status: str) -> str:
    """停用或恢复一个租户

    Args:
        tenant_id: 租户 ID（整数）
        status: 只能是 `active`（恢复）或 `suspended`（停用）

    会直接生效，执行后宿主会在本频道发一条回执。
    """
    grant = _write_grant(ctx)
    if grant is None:
        return UNAVAILABLE
    outcome = await writes.set_tenant_status(tenant_id, status)
    return await _finish_write(ctx, grant, outcome)


async def ops_set_channel_active(ctx: AgentCtx, chat_key: str, active: bool) -> str:
    """启用或停用一个频道，使其恢复 / 停止处理消息

    Args:
        chat_key: 完整会话标识
        active: True 启用、False 停用

    会直接生效，执行后宿主会在本频道发一条回执。
    """
    grant = _write_grant(ctx)
    if grant is None:
        return UNAVAILABLE
    outcome = await writes.set_channel_active(chat_key, active)
    return await _finish_write(ctx, grant, outcome)


async def ops_quota_bonus(ctx: AgentCtx, chat_key: str, amount: int) -> str:
    """给某个频道今天临时加一笔额度，次日作废

    Args:
        chat_key: 完整会话标识
        amount: 正整数，加多少

    会直接生效，执行后宿主会在本频道发一条回执。
    """
    grant = _write_grant(ctx)
    if grant is None:
        return UNAVAILABLE
    outcome = await writes.add_quota_bonus(chat_key, amount)
    return await _finish_write(ctx, grant, outcome)


async def ops_tools_off(ctx: AgentCtx) -> str:
    """关闭运维工具

    运维人员说要关就关，不需要再确认。关掉之后本轮剩余的写调用也会被拒绝；
    重新打开只能在后台或用 `/na_ops tools on`。

    会直接生效，执行后宿主会在本频道发一条回执。
    """
    grant = _write_grant(ctx)
    if grant is None:
        return UNAVAILABLE
    outcome = await writes.set_tools_enabled(False)
    return await _finish_write(ctx, grant, outcome)


# 工具名 -> (实现, 是否写操作)。名字必须 `ops_` 开头：`routers/rpc.py` 靠前缀决定要不要复核授权，
# 插件方法名里出现同名前缀会被判成名字冲突（有专门用例钉住）。
OPS_TOOL_FUNCS: Dict[str, Any] = {
    "ops_overview": ops_overview,
    "ops_list_instances": ops_list_instances,
    "ops_instance_status": ops_instance_status,
    "ops_list_tenants": ops_list_tenants,
    "ops_find": ops_find,
    "ops_recent_errors": ops_recent_errors,
    "ops_usage_today": ops_usage_today,
    "ops_channel_quota": ops_channel_quota,
    "ops_audit_recent": ops_audit_recent,
    "ops_set_instance_enabled": ops_set_instance_enabled,
    "ops_restart_protocol": ops_restart_protocol,
    "ops_set_tenant_status": ops_set_tenant_status,
    "ops_set_channel_active": ops_set_channel_active,
    "ops_quota_bonus": ops_quota_bonus,
    "ops_tools_off": ops_tools_off,
}

# 写工具：`provider.is_write_method` 与 `round_grant.authorize_call` 都读这一份
OPS_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "ops_set_instance_enabled",
        "ops_restart_protocol",
        "ops_set_tenant_status",
        "ops_set_channel_active",
        "ops_quota_bonus",
        "ops_tools_off",
    },
)

# 不计次数、读过外部内容之后也照样放行的写工具：关掉只会缩小权限
OPS_LIMIT_EXEMPT_TOOLS: frozenset[str] = frozenset({"ops_tools_off"})


__all__ = [
    "MAX_AUDIT_LINES",
    "MAX_IDENT_CHARS",
    "MAX_NAME_CHARS",
    "OPS_LIMIT_EXEMPT_TOOLS",
    "OPS_TOOL_FUNCS",
    "OPS_WRITE_TOOLS",
    "UNAVAILABLE",
    "ops_audit_recent",
    "ops_channel_quota",
    "ops_find",
    "ops_instance_status",
    "ops_list_instances",
    "ops_list_tenants",
    "ops_overview",
    "ops_quota_bonus",
    "ops_recent_errors",
    "ops_restart_protocol",
    "ops_set_channel_active",
    "ops_set_instance_enabled",
    "ops_set_tenant_status",
    "ops_tools_off",
    "ops_usage_today",
]
