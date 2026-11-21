"""频道每日额度的控制台接口

## 两组写入接口是**两组模型**

平台方字段（开关、单位、额度、超额动作、提示语）只有平台方能写；租户能写的只有
`tenant_*` 那几列。这不是同一组字段加一层校验，而是两个请求模型：租户的模型里根本
没有平台方字段，于是「租户写平台方字段」在解析请求体那一步就不成立，不依赖任何一处
if 写对。理由与 `models/db_channel_quota.py` 的模块文档一致——平台方的承诺不能落在
租户能写的那一层。

## 作用域与越权

与用量页同一条规矩：读接口一律 `Role.Admin`，可见范围由**租户作用域**而不是角色决定；
越权一律 **404**（`assert_instance_accessible` / `assert_channel_accessible`），
403 会把平台方的实例与频道清单变成可枚举的。

## 列表在作用域内的频道全集上算状态

频道数在几百的量级，而状态（暂停 / 告警 / 正常）是**算出来的**：要按当天的计数行与
三层策略逐频道算，没法在 SQL 里排序或筛选。所以先把作用域内的频道全集取出来、算完状态、
筛完、按百分比倒序，再分页。数据量到几千条时这里要换成物化视图，现在不值得。
"""

from __future__ import annotations

import datetime
from functools import reduce
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from tortoise.expressions import Q
from tortoise.functions import Sum

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.chat_channel import assert_channel_accessible
from nekro_agent.schemas.channel_quota import (
    ChannelQuotaBonusRequest,
    ChannelQuotaBonusResponse,
    ChannelQuotaDailyItem,
    ChannelQuotaDailyListResponse,
    ChannelQuotaHistoryDay,
    ChannelQuotaHistoryResponse,
    ChannelQuotaPolicyInfo,
    ChannelQuotaPolicySummary,
    ChannelQuotaRecountResponse,
    ChannelQuotaSceneUsage,
    GlobalQuotaPolicyUpdate,
    ScopedQuotaPolicyUpdate,
    TenantChannelQuotaInfo,
    TenantChannelQuotaUpdate,
    TenantInstanceQuotaInfo,
    TenantInstanceQuotaUpdate,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services.channel_quota import counter, reconcile
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import (
    SCOPE_CHANNEL,
    SCOPE_GLOBAL,
    SCOPE_INSTANCE,
    EffectivePolicy,
    resolve_policy,
)
from nekro_agent.services.channel_quota.service import QuotaStatus, build_status
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    accessible_instance_refs,
    assert_instance_accessible,
    assert_tenant_accessible,
    get_tenant_scope,
    instance_refs_filter,
    require_platform_owner,
)

router = APIRouter(tags=["Console"])

# 列表页一次最多取这么多频道。上限 200 与用量页的 `page_size` 一致
MAX_PAGE_SIZE = 200
# 历史接口最多看这么多天
MAX_HISTORY_DAYS = 30


# ---------------------------------------------------------------------------
# 作用域
# ---------------------------------------------------------------------------


async def _visible_channels(user: DBUser) -> List[DBChatChannel]:
    """当前身份可见的频道全集

    平台方不受限；租户只看得到自己名下实例上的频道，按 `(adapter_key, instance_key)`
    成对过滤（同一个 `instance_key` 可以在不同适配器下分属两个租户）。
    """
    if get_tenant_scope(user) is None:
        return list(await DBChatChannel.all().order_by("id"))
    return list(await DBChatChannel.filter(instance_refs_filter(await accessible_instance_refs(user))).order_by("id"))


async def _channel_or_404(chat_key: str) -> DBChatChannel:
    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        raise NotFoundError(resource="聊天频道")
    return channel


async def _instance_or_404(adapter_key: str, instance_key: str) -> DBAdapterInstance:
    instance = (
        await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
    )
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


async def _tenant_labels(channels: List[DBChatChannel]) -> Tuple[Dict[Tuple[str, str], Optional[int]], Dict[int, str]]:
    """一次性取出这批频道所属实例的租户归属与租户名

    逐频道查实例会把列表接口变成 N 次查询；这里按频道用到的 `(适配器, 实例)` 去重后
    一次取完，再一次性取租户名。
    """
    refs = {(channel.adapter_key, channel.instance_key) for channel in channels if channel.instance_key}
    if not refs:
        return {}, {}
    conditions = [Q(adapter_key=adapter, instance_key=instance) for adapter, instance in refs]
    owner: Dict[Tuple[str, str], Optional[int]] = {}
    tenant_ids = set()

    for row in await DBAdapterInstance.filter(reduce(lambda left, right: left | right, conditions)):
        owner[(row.adapter_key, row.instance_key)] = row.tenant_id
        if row.tenant_id is not None:
            tenant_ids.add(row.tenant_id)
    names: Dict[int, str] = {}
    if tenant_ids:
        for tenant in await DBTenant.filter(id__in=list(tenant_ids)):
            names[tenant.id] = tenant.display_name or tenant.name
    return owner, names


def _tenant_name_of(
    channel: DBChatChannel,
    owner: Dict[Tuple[str, str], Optional[int]],
    names: Dict[int, str],
) -> Tuple[Optional[int], str]:
    tenant_id = owner.get((channel.adapter_key, channel.instance_key))
    if tenant_id is None:
        return None, ""
    return tenant_id, names.get(tenant_id, "")


# ---------------------------------------------------------------------------
# 状态拼装
# ---------------------------------------------------------------------------


def _status_of(policy: EffectivePolicy, daily: Optional[DBChannelQuotaDaily]) -> QuotaStatus:
    return build_status(policy, daily)


def _remaining_of(status: QuotaStatus) -> Optional[int]:
    """剩余额度；不限时返回 None（前端据此显示「不限」而不是一个巨大的数字）"""
    if status.allowed <= 0:
        return None
    return max(status.allowed - status.used, 0)


def _daily_item(
    channel: DBChatChannel,
    status: QuotaStatus,
    owner: Dict[Tuple[str, str], Optional[int]],
    names: Dict[int, str],
) -> ChannelQuotaDailyItem:
    tenant_id, tenant_name = _tenant_name_of(channel, owner, names)
    return ChannelQuotaDailyItem(
        chat_key=channel.chat_key,
        channel_name=channel.channel_name or "",
        chat_type=channel.channel_type or "",
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        unit=status.unit,
        used=status.used,
        limit=status.limit,
        bonus=status.bonus,
        remaining=_remaining_of(status),
        percent=status.percent,
        state=status.state,
        action=status.action,
    )


# ---------------------------------------------------------------------------
# 每日用量列表
# ---------------------------------------------------------------------------


@router.get("/channel-quota/daily", summary="频道每日额度用量列表")
@require_role(Role.Admin)
async def list_channel_quota_daily(
    day: Optional[str] = Query(default=None, description="日期键 YYYY-MM-DD；留空表示今天（按额度时区）"),
    tenant_id: Optional[int] = Query(default=None, description="租户作用域；留空时平台方表示全部租户"),
    adapter_key: str = Query(default="", description="实例作用域：适配器标识；与 instance_key 成对使用"),
    instance_key: str = Query(default="", description="实例作用域：实例标识"),
    state: str = Query(default="", description="按状态过滤：normal / warning / exceeded / paused / unlimited / disabled"),
    chat_type: str = Query(default="", description="按频道类型过滤：group / private"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChannelQuotaDailyListResponse:
    """按作用域列出频道当天的用量与状态，按用量百分比从高到低排"""
    scope = get_tenant_scope(_current_user)
    if tenant_id is not None:
        # 平台方传一个不存在的租户同样 404；租户传别人的租户号也走这一条
        await assert_tenant_accessible(_current_user, tenant_id)
    if instance_key:
        await assert_instance_accessible(_current_user, instance_key, adapter_key=adapter_key)

    key = day or counter.quota_day()
    channels = await _visible_channels(_current_user)

    owner, names = await _tenant_labels(channels)
    effective_tenant = scope if scope is not None else tenant_id
    if effective_tenant is not None:
        channels = [
            channel for channel in channels if owner.get((channel.adapter_key, channel.instance_key)) == effective_tenant
        ]
    if instance_key:
        channels = [
            channel
            for channel in channels
            if channel.instance_key == instance_key and (not adapter_key or channel.adapter_key == adapter_key)
        ]
    if chat_type == "group":
        channels = [channel for channel in channels if channel.channel_type == "group"]
    elif chat_type == "private":
        channels = [channel for channel in channels if channel.channel_type != "group"]

    # 当天的计数行一次取完，避免逐频道查库；没有行的频道按 0 算，**不建行**
    daily_rows = await counter.get_daily_bulk([channel.chat_key for channel in channels], key)

    items: List[ChannelQuotaDailyItem] = []
    for channel in channels:
        effective = await resolve_policy(
            channel.adapter_key,
            channel.instance_key,
            channel.chat_key,
            channel.channel_type,
        )
        status = _status_of(effective, daily_rows.get(channel.chat_key))
        if state and status.state != state:
            continue
        items.append(_daily_item(channel, status, owner, names))

    items.sort(key=lambda item: (item.percent, item.used), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return ChannelQuotaDailyListResponse(
        total=total,
        page=page,
        page_size=page_size,
        day=key,
        items=items[start : start + page_size],
    )


# ---------------------------------------------------------------------------
# 单频道今日状态
# ---------------------------------------------------------------------------


@router.get("/channel-quota/channels/{chat_key}/status", summary="单个频道今天的额度状态")
@require_role(Role.Admin)
async def get_channel_quota_status(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChannelQuotaDailyItem:
    """频道详情页顶部那一行用的单频道状态

    与列表接口用**同一套**状态拼装（`resolve_policy` + `build_status` + `_daily_item`），
    否则同一时刻「列表里的用量」和「详情页那一行」会给出两个数字。

    功能关着时照样返回真实计数：用量是观测，开关只决定拦不拦，不决定记不记。
    计数行不存在按 0 算，**不建行**——读一条展示接口不该写库。
    """
    await assert_channel_accessible(_current_user, chat_key)
    channel = await _channel_or_404(chat_key)

    key = counter.quota_day()
    effective = await resolve_policy(channel.adapter_key, channel.instance_key, chat_key, channel.channel_type)
    daily = await counter.get_daily(chat_key, key)
    owner, names = await _tenant_labels([channel])
    return _daily_item(channel, _status_of(effective, daily), owner, names)


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------


def _recent_day_keys(days: int) -> List[str]:
    """最近 `days` 天的日期键，从旧到新（含今天）"""
    tz = counter.resolve_timezone()
    today = datetime.datetime.now(tz).date()
    return [(today - datetime.timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _day_window(day_keys: List[str]) -> Tuple[datetime.datetime, datetime.datetime]:
    """把日期键的闭区间换算成 UTC 的 `[start, end)` 时间窗

    单日与多日只差在这一层的首尾取法上；换算本身（含时区名非法时的回落）只有
    `reconcile.day_window` 那一份实现，重算与历史才对得上同一条日界。
    """
    start, _ = reconcile.day_window(day_keys[0])
    _, end = reconcile.day_window(day_keys[-1])
    return start, end


@router.get("/channel-quota/channels/{chat_key}/history", summary="频道每日额度历史")
@require_role(Role.Admin)
async def get_channel_quota_history(
    chat_key: str,
    days: int = Query(default=7, ge=1, le=MAX_HISTORY_DAYS),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChannelQuotaHistoryResponse:
    """逐天列出回复次数与 token 计数，并附这段时间内账本按场景的 token 合计

    租户看到的内容里**不含任何费用与倍率字段**：额度承诺的单位是 token 本身，
    费用是平台方的定价，与「这个群今天还剩多少额度」不是同一个问题。
    """
    await assert_channel_accessible(_current_user, chat_key)
    channel = await _channel_or_404(chat_key)

    day_keys = _recent_day_keys(days)
    rows = await DBChannelQuotaDaily.filter(chat_key=chat_key, day__in=day_keys)
    by_day = {row.day: row for row in rows}

    history: List[ChannelQuotaHistoryDay] = []
    for key in day_keys:
        row = by_day.get(key)
        history.append(
            ChannelQuotaHistoryDay(
                day=key,
                replies=int(row.replies or 0) if row is not None else 0,
                tokens_all=int(row.tokens_all or 0) if row is not None else 0,
                tokens_reply=int(row.tokens_reply or 0) if row is not None else 0,
                bonus=int(row.bonus or 0) if row is not None else 0,
            ),
        )

    start_time, end_time = _day_window(day_keys)
    scene_rows = await (
        DBLLMUsageRecord.filter(chat_key=chat_key, create_time__gte=start_time, create_time__lt=end_time)
        .annotate(agg_token_total=Sum("token_total"))
        .group_by("scene")
        .order_by("-agg_token_total")
        .values("scene", "agg_token_total")
    )
    by_scene = [
        ChannelQuotaSceneUsage(scene=str(row.get("scene") or ""), token_total=int(row.get("agg_token_total") or 0))
        for row in scene_rows
    ]

    effective = await resolve_policy(channel.adapter_key, channel.instance_key, channel.chat_key, channel.channel_type)
    return ChannelQuotaHistoryResponse(
        chat_key=chat_key,
        days=history,
        by_scene=by_scene,
        policy=ChannelQuotaPolicySummary(
            unit=effective.unit,
            platform_limit=effective.platform_limit,
            tenant_limit=effective.tenant_limit,
            effective_limit=effective.effective_limit,
            over_limit_action=effective.over_limit_action,
            pause_message=effective.pause_message,
        ),
    )


# ---------------------------------------------------------------------------
# 平台方：三层策略
# ---------------------------------------------------------------------------


def _policy_info(row: Optional[DBChannelQuotaPolicy], scope: str, adapter_key: str, instance_key: str, chat_key: str) -> ChannelQuotaPolicyInfo:
    if row is None:
        return ChannelQuotaPolicyInfo(
            scope=scope,
            adapter_key=adapter_key,
            instance_key=instance_key,
            chat_key=chat_key,
            enabled=False if scope == SCOPE_GLOBAL else None,
            unit=policy_module.UNIT_REPLIES if scope == SCOPE_GLOBAL else None,
            group_daily_limit=0 if scope == SCOPE_GLOBAL else None,
            private_daily_limit=0 if scope == SCOPE_GLOBAL else None,
            over_limit_action=policy_module.ACTION_ALERT if scope == SCOPE_GLOBAL else None,
            pause_message=None,
            updated_by="",
            update_time=None,
        )
    return ChannelQuotaPolicyInfo(
        scope=row.scope,
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        chat_key=row.chat_key,
        enabled=row.enabled,
        unit=row.unit,
        group_daily_limit=row.group_daily_limit,
        private_daily_limit=row.private_daily_limit,
        over_limit_action=row.over_limit_action,
        pause_message=row.pause_message,
        updated_by=row.updated_by,
        update_time=row.update_time.isoformat() if row.update_time else None,
    )


async def _policy_row(scope: str, adapter_key: str, instance_key: str, chat_key: str) -> Optional[DBChannelQuotaPolicy]:
    return await DBChannelQuotaPolicy.filter(
        scope=scope,
        adapter_key=adapter_key,
        instance_key=instance_key,
        chat_key=chat_key,
    ).order_by("id").first()


async def _upsert_policy(
    *,
    scope: str,
    adapter_key: str,
    instance_key: str,
    chat_key: str,
    fields: Dict[str, object],
    updated_by: str,
) -> DBChannelQuotaPolicy:
    """写入一层的平台方字段并让缓存失效

    `fields` 里出现 `None` 是**有意义的**：它表示「这一层不设，跟上一层」，
    所以这里不能用「跳过 None」的写法，必须原样写进去。
    """
    row = await _policy_row(scope, adapter_key, instance_key, chat_key)
    if row is None:
        row = await DBChannelQuotaPolicy.create(
            scope=scope,
            adapter_key=adapter_key,
            instance_key=instance_key,
            chat_key=chat_key,
            updated_by=updated_by,
            **fields,
        )
    else:
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_by = updated_by
        await row.save()
    policy_module.invalidate_policy_cache()
    return row


@router.get("/channel-quota/policy", summary="读取全局频道额度策略")
async def get_global_quota_policy(
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaPolicyInfo:
    """全局那一行；不存在时返回默认值（功能关闭、单位回复数、两种额度 0、只告警）"""
    row = await _policy_row(SCOPE_GLOBAL, "", "", "")
    return _policy_info(row, SCOPE_GLOBAL, "", "", "")


@router.put("/channel-quota/policy", summary="写入全局频道额度策略")
async def update_global_quota_policy(
    body: GlobalQuotaPolicyUpdate,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaPolicyInfo:
    """全局是最后一层：开关、单位、超额动作都必须给值（模型上已经不允许留空）"""
    row = await _upsert_policy(
        scope=SCOPE_GLOBAL,
        adapter_key="",
        instance_key="",
        chat_key="",
        fields={
            "enabled": body.enabled,
            "unit": body.unit,
            "group_daily_limit": body.group_daily_limit if body.group_daily_limit is not None else 0,
            "private_daily_limit": body.private_daily_limit if body.private_daily_limit is not None else 0,
            "over_limit_action": body.over_limit_action,
            "pause_message": body.pause_message,
        },
        updated_by=_current_user.username,
    )
    return _policy_info(row, SCOPE_GLOBAL, "", "", "")


@router.get("/channel-quota/policy/instance/{adapter_key}/{instance_key}", summary="读取实例层频道额度策略")
async def get_instance_quota_policy(
    adapter_key: str,
    instance_key: str,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaPolicyInfo:
    """实例那一行；没有行时各列为 null，表示全部跟全局"""
    await _instance_or_404(adapter_key, instance_key)
    row = await _policy_row(SCOPE_INSTANCE, adapter_key, instance_key, "")
    return _policy_info(row, SCOPE_INSTANCE, adapter_key, instance_key, "")


@router.put("/channel-quota/policy/instance/{adapter_key}/{instance_key}", summary="写入实例层频道额度策略")
async def update_instance_quota_policy(
    adapter_key: str,
    instance_key: str,
    body: ScopedQuotaPolicyUpdate,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaPolicyInfo:
    """实例层只写想改的那几列，其余留 null 表示继续继承全局"""
    await _instance_or_404(adapter_key, instance_key)
    row = await _upsert_policy(
        scope=SCOPE_INSTANCE,
        adapter_key=adapter_key,
        instance_key=instance_key,
        chat_key="",
        fields={
            "enabled": body.enabled,
            "unit": body.unit,
            "group_daily_limit": body.group_daily_limit,
            "private_daily_limit": body.private_daily_limit,
            "over_limit_action": body.over_limit_action,
            "pause_message": body.pause_message,
        },
        updated_by=_current_user.username,
    )
    return _policy_info(row, SCOPE_INSTANCE, adapter_key, instance_key, "")


@router.get("/channel-quota/policy/channel/{chat_key}", summary="读取频道层频道额度策略")
async def get_channel_quota_policy(
    chat_key: str,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaPolicyInfo:
    """频道那一行；没有行时各列为 null，表示全部跟上一层"""
    channel = await _channel_or_404(chat_key)
    row = await _policy_row(SCOPE_CHANNEL, channel.adapter_key, channel.instance_key, chat_key)
    return _policy_info(row, SCOPE_CHANNEL, channel.adapter_key, channel.instance_key, chat_key)


@router.put("/channel-quota/policy/channel/{chat_key}", summary="写入频道层频道额度策略")
async def update_channel_quota_policy(
    chat_key: str,
    body: ScopedQuotaPolicyUpdate,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaPolicyInfo:
    """频道层只写想改的那几列，其余留 null 表示继续继承"""
    channel = await _channel_or_404(chat_key)
    row = await _upsert_policy(
        scope=SCOPE_CHANNEL,
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        chat_key=chat_key,
        fields={
            "enabled": body.enabled,
            "unit": body.unit,
            "group_daily_limit": body.group_daily_limit,
            "private_daily_limit": body.private_daily_limit,
            "over_limit_action": body.over_limit_action,
            "pause_message": body.pause_message,
        },
        updated_by=_current_user.username,
    )
    return _policy_info(row, SCOPE_CHANNEL, channel.adapter_key, channel.instance_key, chat_key)


# ---------------------------------------------------------------------------
# 租户：自己名下的「我的额度」
# ---------------------------------------------------------------------------


def _assert_tenant_limit_within_platform(value: Optional[int], platform_limit: int) -> None:
    """租户值不得超过平台方额度；平台方额度为 0（不限）时任意正整数都行"""
    if value is None or platform_limit <= 0:
        return
    if value > platform_limit:
        raise ValidationError(reason=f"额度不能高于平台方给的上限 {platform_limit}")


async def _platform_limits(adapter_key: str, instance_key: str, chat_key: str) -> Tuple[EffectivePolicy, EffectivePolicy]:
    """同一作用域下群与私聊各自的生效策略（取其中的平台方额度做上限校验）"""
    group = await resolve_policy(adapter_key, instance_key, chat_key, "group")
    private = await resolve_policy(adapter_key, instance_key, chat_key, "private")
    return group, private


@router.get("/channel-quota/tenant/instance/{adapter_key}/{instance_key}", summary="读取实例层租户额度")
@require_role(Role.Admin)
async def get_tenant_instance_quota(
    adapter_key: str,
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TenantInstanceQuotaInfo:
    """租户在实例层设的额度与提示语，附带平台方那一层的额度供对照"""
    await assert_instance_accessible(_current_user, instance_key, adapter_key=adapter_key)
    row = await _policy_row(SCOPE_INSTANCE, adapter_key, instance_key, "")
    group, private = await _platform_limits(adapter_key, instance_key, "")
    return TenantInstanceQuotaInfo(
        adapter_key=adapter_key,
        instance_key=instance_key,
        unit=group.unit,
        group_daily_limit=row.tenant_group_daily_limit if row is not None else None,
        private_daily_limit=row.tenant_private_daily_limit if row is not None else None,
        pause_message=row.tenant_pause_message if row is not None else None,
        platform_group_daily_limit=group.platform_limit,
        platform_private_daily_limit=private.platform_limit,
    )


@router.put("/channel-quota/tenant/instance/{adapter_key}/{instance_key}", summary="写入实例层租户额度")
@require_role(Role.Admin)
async def update_tenant_instance_quota(
    adapter_key: str,
    instance_key: str,
    body: TenantInstanceQuotaUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TenantInstanceQuotaInfo:
    """租户在平台方给的额度以内调低；平台方也能调（它也是租户字段的合法写入者）"""
    await assert_instance_accessible(_current_user, instance_key, adapter_key=adapter_key)
    group, private = await _platform_limits(adapter_key, instance_key, "")
    _assert_tenant_limit_within_platform(body.group_daily_limit, group.platform_limit)
    _assert_tenant_limit_within_platform(body.private_daily_limit, private.platform_limit)

    row = await _policy_row(SCOPE_INSTANCE, adapter_key, instance_key, "")
    if row is None:
        row = await DBChannelQuotaPolicy.create(
            scope=SCOPE_INSTANCE,
            adapter_key=adapter_key,
            instance_key=instance_key,
            chat_key="",
            updated_by=_current_user.username,
        )
    row.tenant_group_daily_limit = body.group_daily_limit
    row.tenant_private_daily_limit = body.private_daily_limit
    row.tenant_pause_message = body.pause_message
    # 单位快照：租户填的数字只在「他填的那一刻那个单位」下有意义，见 `policy.py` 的模块文档
    row.tenant_limit_unit = group.unit
    row.updated_by = _current_user.username
    await row.save()
    policy_module.invalidate_policy_cache()

    return TenantInstanceQuotaInfo(
        adapter_key=adapter_key,
        instance_key=instance_key,
        unit=group.unit,
        group_daily_limit=row.tenant_group_daily_limit,
        private_daily_limit=row.tenant_private_daily_limit,
        pause_message=row.tenant_pause_message,
        platform_group_daily_limit=group.platform_limit,
        platform_private_daily_limit=private.platform_limit,
    )


@router.get("/channel-quota/tenant/channel/{chat_key}", summary="读取频道层租户额度")
@require_role(Role.Admin)
async def get_tenant_channel_quota(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TenantChannelQuotaInfo:
    """租户在这个频道上设的额度与提示语，附带平台方额度与生效额度供对照"""
    await assert_channel_accessible(_current_user, chat_key)
    channel = await _channel_or_404(chat_key)
    row = await _policy_row(SCOPE_CHANNEL, channel.adapter_key, channel.instance_key, chat_key)
    is_group = channel.channel_type == "group"
    effective = await resolve_policy(channel.adapter_key, channel.instance_key, chat_key, channel.channel_type)
    daily_limit = None
    if row is not None:
        daily_limit = row.tenant_group_daily_limit if is_group else row.tenant_private_daily_limit
    return TenantChannelQuotaInfo(
        chat_key=chat_key,
        chat_type=channel.channel_type or "",
        unit=effective.unit,
        daily_limit=daily_limit,
        pause_message=row.tenant_pause_message if row is not None else None,
        platform_limit=effective.platform_limit,
        effective_limit=effective.effective_limit,
    )


@router.put("/channel-quota/tenant/channel/{chat_key}", summary="写入频道层租户额度")
@require_role(Role.Admin)
async def update_tenant_channel_quota(
    chat_key: str,
    body: TenantChannelQuotaUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TenantChannelQuotaInfo:
    """`daily_limit` 按这个频道自己的类型写进群列或私聊列"""
    await assert_channel_accessible(_current_user, chat_key)
    channel = await _channel_or_404(chat_key)
    is_group = channel.channel_type == "group"
    effective = await resolve_policy(channel.adapter_key, channel.instance_key, chat_key, channel.channel_type)
    _assert_tenant_limit_within_platform(body.daily_limit, effective.platform_limit)

    row = await _policy_row(SCOPE_CHANNEL, channel.adapter_key, channel.instance_key, chat_key)
    if row is None:
        row = await DBChannelQuotaPolicy.create(
            scope=SCOPE_CHANNEL,
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
            chat_key=chat_key,
            updated_by=_current_user.username,
        )
    if is_group:
        row.tenant_group_daily_limit = body.daily_limit
    else:
        row.tenant_private_daily_limit = body.daily_limit
    row.tenant_pause_message = body.pause_message
    row.tenant_limit_unit = effective.unit
    row.updated_by = _current_user.username
    await row.save()
    policy_module.invalidate_policy_cache()

    daily_limit = row.tenant_group_daily_limit if is_group else row.tenant_private_daily_limit
    return TenantChannelQuotaInfo(
        chat_key=chat_key,
        chat_type=channel.channel_type or "",
        unit=effective.unit,
        daily_limit=daily_limit,
        pause_message=row.tenant_pause_message,
        platform_limit=effective.platform_limit,
        effective_limit=effective.effective_limit,
    )


# ---------------------------------------------------------------------------
# 临时加额
# ---------------------------------------------------------------------------


@router.post("/channel-quota/channels/{chat_key}/bonus", summary="给频道今天的额度临时加额")
async def add_channel_quota_bonus(
    chat_key: str,
    body: ChannelQuotaBonusRequest,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaBonusResponse:
    """原子地给今天的 `bonus` 加上 `amount`；没有计数行就建

    加额是平台方的兜底动作（客户临时要跑一场活动），所以只有平台方能调。
    加完之后返回新的状态，调用方不用再查一次。
    """
    channel = await _channel_or_404(chat_key)
    key = counter.quota_day()
    await counter.add_bonus(chat_key, key, body.amount)

    effective = await resolve_policy(channel.adapter_key, channel.instance_key, chat_key, channel.channel_type)
    daily = await counter.get_daily(chat_key, key)
    status = _status_of(effective, daily)
    owner, names = await _tenant_labels([channel])
    return ChannelQuotaBonusResponse(
        chat_key=chat_key,
        day=key,
        bonus=status.bonus,
        status=_daily_item(channel, status, owner, names),
    )


# ---------------------------------------------------------------------------
# 按账本重算
# ---------------------------------------------------------------------------


def _day_or_400(day: Optional[str]) -> str:
    """把查询参数里的日期键规范成 `YYYY-MM-DD`；留空表示今天

    格式不对是调用方的问题（400），不是服务端故障（500）：`day_window` 里那个
    `fromisoformat` 抛 `ValueError` 一路冒上去会变成 500，看日志的人会以为是对账坏了。
    """
    if not day:
        return counter.quota_day()
    try:
        return reconcile.normalize_day(day)
    except ValueError as exc:
        raise ValidationError(reason=str(exc)) from exc


@router.post("/channel-quota/channels/{chat_key}/recount", summary="按账本重算某个频道某一天的 token")
async def recount_channel_quota(
    chat_key: str,
    day: Optional[str] = Query(default=None, description="日期键 YYYY-MM-DD；留空表示今天（按额度时区）"),
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaDailyItem:
    """按账本重算这一个频道的两列 token，并返回重算之后的状态

    平台方的对账动作：答复「这个群今天到底烧了多少」时以账本为准，不信任计数缓存。
    回复次数不重算（账本里没有「回合」这一维），所以这里返回的 `used` 在回复次数单位下
    与重算之前完全一致——这不是漏了，是那一列没有可重算的来源。
    """
    channel = await _channel_or_404(chat_key)
    key = _day_or_400(day)
    await reconcile.recount_tokens(key, chat_key)

    effective = await resolve_policy(channel.adapter_key, channel.instance_key, chat_key, channel.channel_type)
    daily = await counter.get_daily(chat_key, key)
    status = _status_of(effective, daily)
    owner, names = await _tenant_labels([channel])
    return _daily_item(channel, status, owner, names)


@router.post("/channel-quota/recount", summary="按账本重算某一天所有频道的 token")
async def recount_all_channel_quota(
    day: Optional[str] = Query(default=None, description="日期键 YYYY-MM-DD；留空表示今天（按额度时区）"),
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ChannelQuotaRecountResponse:
    """按账本重算这一天所有有流水的频道，返回处理了几个频道

    只报数量：前端拿到之后要刷新列表，列表本身就是逐频道的数字，这里再回一份会让
    「刚刷新的列表」和「接口刚返回的那份」互相打架。
    """
    key = _day_or_400(day)
    channels = await reconcile.recount_tokens(key)
    return ChannelQuotaRecountResponse(day=key, channels=channels)


__all__ = ["router"]
