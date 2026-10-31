"""总后台实例配额路由（需求文档风险登记 RK6）

`AI_CHAT_DAILY_REPLY_LIMIT` 一直是 overridable 字段、能按实例设，但此前唯一的入口是聊天命令
`/quota_set`，而它写的还是**频道层**。几十个实例时既没有总览，也没有批量设置，运维只能一个群
一个群地去发命令。这里补上按实例的查看与设置。

## 权限模型

- 读与写都是 `Role.Admin`：定制版客户要能在自己的后台管自己实例的配额。
  可见范围由租户作用域而不是 `Role` 决定，越权一律 404（见 `assert_instance_accessible`）。
- **平台方上限（ceiling）只有平台方能写**。租户可以把自己的配额往下调，但不能往上突破，
  这是计费边界：租户能自助提额等于自助提高平台方的 LLM 成本。
  上限落在 `DBAdapterInstance.metadata_json` 而不是覆盖配置层，正是因为覆盖层是租户可写的。
- 通用配置接口（`PUT /config/instance/...`、`POST /config/batch/...`）是同一条边界上的另一扇门，
  已在 `routers/config.py` 的租户写入护栏里接上同一个判定，否则本文件拦得再严也可以绕开。
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import (
    InstanceConcurrencyInfo,
    InstanceQuotaBatchRequest,
    InstanceQuotaBatchResponse,
    InstanceQuotaInfo,
    InstanceQuotaListResponse,
    InstanceQuotaUpdateRequest,
)
from nekro_agent.schemas.errors import PermissionDeniedError, ValidationError
from nekro_agent.services.console.instance_manager import STATUS_TOMBSTONE
from nekro_agent.services.instance_concurrency import scheduler
from nekro_agent.services.quota_service import (
    collect_instance_quota_usage,
    is_quota_within_ceiling,
    load_quota_ceiling,
    resolve_instance_daily_limit,
    save_quota_ceiling,
    set_instance_daily_limit,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope, is_platform_owner

router = APIRouter(tags=["Console"])


@router.get("/quota/instances", summary="获取实例配额列表")
@require_role(Role.Admin)
async def list_instance_quotas(
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceQuotaListResponse:
    """按实例列出当前配额、今日已用量与并发运行状况

    平台方看全部；租户只看自己名下的实例。
    """
    scope = get_tenant_scope(_current_user)
    query = DBAdapterInstance.exclude(status=STATUS_TOMBSTONE)
    if scope is not None:
        query = query.filter(tenant_id=scope)
    instances = await query.order_by("id")

    limits: Dict[str, int] = {}
    sources: Dict[str, str] = {}
    inherited: Dict[str, int] = {}
    for instance in instances:
        value, source, fallback = await resolve_instance_daily_limit(instance.adapter_key, instance.instance_key)
        limits[instance.instance_key] = value
        sources[instance.instance_key] = source
        inherited[instance.instance_key] = fallback

    usage = await collect_instance_quota_usage([instance.instance_key for instance in instances], limits)
    concurrency = scheduler.stats()
    capacity = scheduler.capacity_snapshot()

    items: List[InstanceQuotaInfo] = []
    for instance in instances:
        key = instance.instance_key
        counts = usage.get(key, {})
        runtime = concurrency.get(key)
        items.append(
            InstanceQuotaInfo(
                instance_key=key,
                display_name=instance.display_name,
                adapter_key=instance.adapter_key,
                tenant_id=instance.tenant_id,
                enabled=instance.enabled,
                daily_limit=limits.get(key, 0),
                limit_source=sources.get(key, "system"),
                inherited_limit=inherited.get(key, 0),
                ceiling=load_quota_ceiling(instance),
                replies_today=counts.get("replies_today", 0),
                active_channels=counts.get("active_channels", 0),
                peak_channel_replies=counts.get("peak_channel_replies", 0),
                channels_at_limit=counts.get("channels_at_limit", 0),
                concurrency=InstanceConcurrencyInfo(
                    active=runtime.active if runtime else 0,
                    queued=runtime.queued if runtime else 0,
                    max_concurrent=runtime.max_concurrent if runtime else 0,
                    queue_limit=runtime.queue_limit if runtime else 0,
                    admitted_total=runtime.admitted_total if runtime else 0,
                    rejected_total=(runtime.rejected_queue_full + runtime.rejected_timeout) if runtime else 0,
                    max_wait_seconds=round(runtime.max_wait_seconds, 3) if runtime else 0.0,
                ),
            ),
        )

    return InstanceQuotaListResponse(
        items=items,
        total=len(items),
        editable_ceiling=is_platform_owner(_current_user),
        global_capacity=capacity["capacity"],
        global_active=capacity["active"],
        global_queued=capacity["queued"],
    )


@router.patch("/quota/instances/{instance_key}", summary="设置实例配额")
@require_role(Role.Admin)
async def update_instance_quota(
    instance_key: str,
    body: InstanceQuotaUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceQuotaInfo:
    """设置单个实例的每日回复上限，以及（仅平台方）平台方上限"""
    instance = await assert_instance_accessible(_current_user, instance_key)
    await _apply_quota_update(_current_user, instance, body)

    value, source, fallback = await resolve_instance_daily_limit(instance.adapter_key, instance.instance_key)
    usage = await collect_instance_quota_usage([instance.instance_key], {instance.instance_key: value})
    counts = usage.get(instance.instance_key, {})
    runtime = scheduler.stats().get(instance.instance_key)
    return InstanceQuotaInfo(
        instance_key=instance.instance_key,
        display_name=instance.display_name,
        adapter_key=instance.adapter_key,
        tenant_id=instance.tenant_id,
        enabled=instance.enabled,
        daily_limit=value,
        limit_source=source,
        inherited_limit=fallback,
        ceiling=load_quota_ceiling(instance),
        replies_today=counts.get("replies_today", 0),
        active_channels=counts.get("active_channels", 0),
        peak_channel_replies=counts.get("peak_channel_replies", 0),
        channels_at_limit=counts.get("channels_at_limit", 0),
        concurrency=InstanceConcurrencyInfo(
            active=runtime.active if runtime else 0,
            queued=runtime.queued if runtime else 0,
            max_concurrent=runtime.max_concurrent if runtime else 0,
            queue_limit=runtime.queue_limit if runtime else 0,
            admitted_total=runtime.admitted_total if runtime else 0,
            rejected_total=(runtime.rejected_queue_full + runtime.rejected_timeout) if runtime else 0,
            max_wait_seconds=round(runtime.max_wait_seconds, 3) if runtime else 0.0,
        ),
    )


@router.post("/quota/instances/batch", summary="批量设置实例配额")
@require_role(Role.Admin)
async def batch_update_instance_quota(
    body: InstanceQuotaBatchRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceQuotaBatchResponse:
    """把同一份改动套用到多个实例

    逐个实例结算：某个实例越权或写盘失败时其余实例照常生效，结果里如实列出成败，
    避免"批量操作报错了，但其实一半已经改了"这种最难排查的中间态。
    """
    if not body.instance_keys:
        raise ValidationError(reason="请至少选择一个实例")

    succeeded: List[str] = []
    failed: List[str] = []
    for instance_key in body.instance_keys:
        try:
            instance = await assert_instance_accessible(_current_user, instance_key)
            await _apply_quota_update(_current_user, instance, body.update)
        except Exception:  # noqa: BLE001 - 单个实例失败不能中断整批
            failed.append(instance_key)
        else:
            succeeded.append(instance_key)

    return InstanceQuotaBatchResponse(
        ok=not failed,
        succeeded=succeeded,
        failed=failed,
        message=f"成功 {len(succeeded)} 个，失败 {len(failed)} 个" if failed else f"已设置 {len(succeeded)} 个实例",
    )


async def _apply_quota_update(user: DBUser, instance: DBAdapterInstance, update: InstanceQuotaUpdateRequest) -> None:
    """把一次配额改动落到某个实例上

    上限先写、配额后写：先收紧上限再校验新配额，才不会出现"同一次请求里用旧上限放行了
    一个超过新上限的配额"。
    """
    if update.set_ceiling:
        if not is_platform_owner(user):
            raise PermissionDeniedError(detail="平台方上限只能由平台方设置")
        await save_quota_ceiling(instance, update.ceiling)

    if not update.clear_override and update.daily_limit is None:
        return

    ceiling = load_quota_ceiling(instance)
    if update.clear_override:
        # 清除覆盖后生效的是上层继承值，它同样可能突破上限（例如全局层是"不限制"）
        _, _, inherited = await resolve_instance_daily_limit(instance.adapter_key, instance.instance_key)
        target: Optional[int] = None
        effective = inherited
    else:
        target = int(update.daily_limit or 0)
        if target < 0:
            raise ValidationError(reason="每日回复上限不能为负数")
        effective = target

    if not is_platform_owner(user) and not is_quota_within_ceiling(effective, ceiling):
        raise PermissionDeniedError(
            detail=f"每日回复上限不得超过平台方为实例 {instance.instance_key} 设定的 {ceiling}（0 表示不限制，同样不被允许）",
        )

    try:
        set_instance_daily_limit(instance.adapter_key, instance.instance_key, target)
    except ValueError as e:
        raise ValidationError(reason=str(e)) from e
