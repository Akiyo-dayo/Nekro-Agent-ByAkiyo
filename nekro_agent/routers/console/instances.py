"""总后台实例 CRUD 路由

实例与协议端容器都是宿主级资源，误操作会直接抹掉一个 Bot 账号的全部数据，
因此**改动实例的端点**（新建、更新、删除、启停、认领）一律要求 `Role.Super`。
租户成员在旧权限体系里被钉死在 `Role.Admin`（见 `services/user/tenant_perm.py` 的
`TENANT_MAX_PERM_LEVEL`），这些端点对他们天然不可达。

两个只读端点（列表与详情）放宽到 `Role.Admin`：定制版客户要能在自己的后台看到自己的实例，
可见范围由租户作用域而不是 `Role` 决定——`Role` 管"能做多危险的操作"，
租户作用域管"看得到哪些资源"，两者是正交的两层。

平台方（`get_tenant_scope` 返回 None）不进入任何过滤分支，未启用多租户时行为与改造前完全一致。
"""

from typing import List, Optional, Set, Tuple

from fastapi import APIRouter, Depends

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import (
    ActionOkResponse,
    InstanceAdoptRequest,
    InstanceAdoptTarget,
    InstanceClaimRequest,
    InstanceClaimResult,
    InstanceCreateRequest,
    InstanceDeleteRequest,
    InstanceInfo,
    InstanceListResponse,
    InstanceMigrationChannelItem,
    InstanceMigrationPreview,
    InstanceMigrationRequest,
    InstanceMigrationResidueItem,
    InstanceMigrationResult,
    InstanceOptionListResponse,
    InstanceUpdateRequest,
    UnclaimedInstanceItem,
    UnclaimedInstanceListResponse,
)
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.chat_key_migration import (
    INSTANCE_MIGRATION_RESIDUE_REASONS,
    InstanceMigrationOutcome,
    migrate_instance_ownership,
    plan_instance_migration,
)
from nekro_agent.services.console.instance_manager import (
    InstanceAdoptOutcome,
    adopt_unclaimed_instance,
    archive_instance,
    claim_unclaimed_as_new_instance,
    collect_instance_stats,
    create_instance,
    describe_instance,
    list_adopt_targets,
    list_instance_options,
    list_instances,
    list_unclaimed_protocol_ends,
    purge_instance,
    resolve_instance_status,
    set_instance_enabled,
    update_instance,
)
from nekro_agent.services.unclaimed_dispatch import unclaimed_pause_reason
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    InstanceRef,
    accessible_instance_refs,
    assert_instance_accessible,
    get_tenant_scope,
)

router = APIRouter(tags=["Console"])
logger = get_sub_logger("console.instances")


async def _visible_instance_keys(user: DBUser) -> Optional[Set[InstanceRef]]:
    """当前身份能看到哪些实例（`(adapter_key, instance_key)`）；None 表示不受限（平台方）

    归属判定只此一处，两个列表端点共用：判据一旦分叉，一边能看到的实例另一边看不到，
    而两边都不会报错。复用 `accessible_instance_refs` 而不是各自再写一次
    `filter(tenant_id=...)`，它与 `/user/me/identity` 下发的可访问实例出自同一条查询。
    成对比较是因为另一个适配器下可以有归别人的同名实例。
    """
    if get_tenant_scope(user) is None:
        return None
    return await accessible_instance_refs(user)


@router.get("/instances", summary="获取实例列表")
@require_role(Role.Admin)
async def get_console_instances(
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceListResponse:
    """获取实例列表及其运行统计

    平台方看全部；租户只看自己名下的实例。
    """
    response = await list_instances()
    accessible = await _visible_instance_keys(_current_user)
    if accessible is None:
        return response

    items = [item for item in response.items if (item.adapter_key, item.instance_key) in accessible]
    return InstanceListResponse(items=items, total=len(items))


@router.get("/instance-options", summary="获取实例切换器候选列表")
@require_role(Role.Admin)
async def get_console_instance_options(
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceOptionListResponse:
    """实例作用域切换器的候选实例（轻量）

    与 `GET /instances` 同一份数据、同一套归属判定（`_visible_instance_keys`），
    只是**不查协议端容器、不做运行统计**——切换器要的只是「有哪些实例、叫什么、
    绑的哪个号、现在什么状态」。差别在大量实例的规模上不是常数级：
    `GET /instances` 会为每个 `builtin_napcat` 实例新建一个 Docker 客户端做一次 inspect。

    ## 为什么路径不是 `/instances/options`

    `options` 本身就是一个合法的 `instance_key`（`^[a-z0-9_]{1,32}$`）。挂在
    `/instances/` 下就要抢在 `/instances/{instance_key}` 之前注册，一旦有人把路由顺序
    调开就会静默变成「查一个叫 options 的实例」；反过来真有实例叫 `options` 时，
    它的详情端点会被本路由永久遮住。两种故障都没有运行时信号，换个不会碰撞的路径即可根除。
    """
    options = await list_instance_options()
    accessible = await _visible_instance_keys(_current_user)
    if accessible is not None:
        options = [option for option in options if (option.adapter_key, option.instance_key) in accessible]
    return InstanceOptionListResponse(items=options, total=len(options))


@router.get("/unclaimed-instances", summary="获取待认领协议端清单")
@require_role(Role.Super)
async def get_console_unclaimed_instances(
    _current_user: DBUser = Depends(get_current_active_user),
) -> UnclaimedInstanceListResponse:
    """列出「已经连上来但没人认领」的协议端，以及可以把它们并进去的既有实例

    这是本轮修复的入口端点。现场形态是：协议端连上时由 `resolve_instance_key()` 自动登记成
    `ob{账号}` 并把流量与频道全部归给它，而管理员手工建的实例是另一行、`provider_account_id`
    为空、永远拿不到那条连接。界面此前把这两行平铺在同一张列表里，管理员看到的是
    「我的实例离线 + 一个不认识的实例」，没有任何地方告诉他真正在线的是旁边那个。

    权限是 `Role.Super`（平台方专属），与认领动作本身一致：认领会移动数据归属并改写账号绑定。
    对租户恒 403 —— 这是**能力边界**而不是资源存在性（拒绝发生在函数体之前、不区分任何实例标识），
    与越权访问某个具体实例返回 404 是两回事。

    ## 为什么路径不是 `/instances/unclaimed`

    与 `/instance-options` 同一条理由：`unclaimed` 本身就是一个合法的 `instance_key`
    （`^[a-z0-9_]{1,32}$`），挂在 `/instances/` 下就要永远抢在 `/instances/{instance_key}`
    之前注册，且会永久遮住一个真叫 `unclaimed` 的实例的详情端点。
    """
    ends = await list_unclaimed_protocol_ends()
    targets = await list_adopt_targets({end.instance.adapter_key for end in ends})
    # 一次聚合拿全部候选目标的会话数：逐个查会在几十个实例的部署上退化成几十次往返
    target_stats = await collect_instance_stats([target.instance_key for target in targets])
    # 未认领实例的消息此刻会不会被丢弃，只取决于部署级令牌的状态（与连接无关的那一半判据，见
    # `services/unclaimed_dispatch.py`），所以整张清单算一次。判不出来（比如某行租户实例的元数据坏了）
    # 按「暂停」显示，不能让管理员要用来认领的这块面板整个报错
    paused_reason = ""
    if ends:
        try:
            paused_reason = await unclaimed_pause_reason()
        except Exception:  # noqa: BLE001 - 见上
            logger.warning("判定未认领实例能否分发失败，待认领清单按「消息暂不处理」显示", exc_info=True)
            paused_reason = "判定出错"

    return UnclaimedInstanceListResponse(
        items=[
            UnclaimedInstanceItem(
                instance_key=end.instance.instance_key,
                adapter_key=end.instance.adapter_key,
                display_name=end.instance.display_name,
                provider_account_id=end.instance.provider_account_id,
                channel_count=end.stats.channel_count,
                message_count_today=end.stats.message_count_today,
                last_active_at=end.stats.last_active_at,
                first_seen_at=end.instance.create_time.isoformat() if end.instance.create_time else None,
                dispatch_paused=bool(paused_reason),
                dispatch_paused_reason=paused_reason,
            )
            for end in ends
        ],
        total=len(ends),
        adopt_targets=[
            InstanceAdoptTarget(
                instance_key=target.instance_key,
                display_name=target.display_name,
                adapter_key=target.adapter_key,
                provider_account_id=target.provider_account_id,
                status=resolve_instance_status(target.status, target.provider, target.provider_account_id),
                channel_count=target_stats[target.instance_key].channel_count
                if target.instance_key in target_stats
                else 0,
            )
            for target in targets
        ],
    )


@router.post("/instances", summary="新建实例")
@require_role(Role.Super)
async def create_console_instance(
    body: InstanceCreateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceInfo:
    """新建实例；`instance_key` 留空时自动生成，创建后不可修改"""
    instance = await create_instance(body)
    return await describe_instance(instance)


@router.get("/instances/{instance_key}", summary="获取实例详情")
@require_role(Role.Admin)
async def get_console_instance(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceInfo:
    """获取单个实例的详情；租户传他人的实例标识一律 404"""
    instance = await assert_instance_accessible(_current_user, instance_key)
    return await describe_instance(instance)


@router.patch("/instances/{instance_key}", summary="更新实例")
@require_role(Role.Super)
async def update_console_instance(
    instance_key: str,
    body: InstanceUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceInfo:
    """更新实例的可变属性（`instance_key` 与 `adapter_key` 不可变）"""
    instance = await assert_instance_accessible(_current_user, instance_key)
    updated = await update_instance(instance, body)
    return await describe_instance(updated)


@router.delete("/instances/{instance_key}", summary="删除实例")
@require_role(Role.Super)
async def delete_console_instance(
    instance_key: str,
    body: InstanceDeleteRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """删除实例

    默认归档：实例置为只读保留，会话、消息、记忆全部保留可查。
    `purge=true` 时彻底删除，需要回填实例标识作二次确认，并级联清除全部关联数据。
    """
    instance = await assert_instance_accessible(_current_user, instance_key)

    if not body.purge:
        await archive_instance(instance)
        return ActionOkResponse(ok=True, message="实例已归档，数据保留可查")

    if body.confirm_instance_key != instance_key:
        raise ValidationError(reason="彻底删除需要回填正确的实例标识作为二次确认")

    counts = await purge_instance(instance)
    return ActionOkResponse(ok=True, message=f"实例已彻底删除，级联清除记录数: {counts}")


@router.post("/instances/{instance_key}/enable", summary="启用实例")
@require_role(Role.Super)
async def enable_console_instance(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """启用实例"""
    instance = await assert_instance_accessible(_current_user, instance_key)
    await set_instance_enabled(instance, enabled=True)
    return ActionOkResponse(ok=True, message="实例已启用")


@router.post("/instances/{instance_key}/disable", summary="停用实例")
@require_role(Role.Super)
async def disable_console_instance(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """停用实例"""
    instance = await assert_instance_accessible(_current_user, instance_key)
    await set_instance_enabled(instance, enabled=False)
    return ActionOkResponse(ok=True, message="实例已停用")


def _describe_migration(outcome: Optional[InstanceMigrationOutcome]) -> Optional[InstanceMigrationResult]:
    """把归属迁移的执行结果转成对外契约；没有搬运时为 None"""
    if outcome is None:
        return None
    return InstanceMigrationResult(
        ok=not outcome.failed_channels,
        source_instance_key=outcome.source_instance_key,
        target_instance_key=outcome.target_instance_key,
        moved_channels=outcome.moved_channels,
        merged_channels=outcome.merged_channels,
        failed_channels=outcome.failed_channels,
        counts=outcome.counts,
        residual=outcome.residual,
        message="",
    )


async def _build_claim_result(outcome: InstanceAdoptOutcome) -> InstanceClaimResult:
    """组装认领结果，并把「半成品」这件事说清楚

    部分频道迁移失败时账号没有过户、源实例也没归档，界面必须显示成一次**未完成**的操作
    （而不是成功），否则管理员会以为已经接管完毕，直到发现历史少了一半。
    """
    migration = _describe_migration(outcome.migration)
    failed = len(outcome.migration.failed_channels) if outcome.migration else 0

    if failed:
        message = (
            f"{failed} 个会话迁移失败，账号未过户、源实例 '{outcome.source_instance_key}' 仍为未认领，"
            "已完成的会话不会回退，重试本操作即可补齐"
        )
    elif outcome.migration is not None:
        message = (
            f"已认领 '{outcome.source_instance_key}' 到实例 '{outcome.target.instance_key}'："
            f"搬迁 {len(outcome.migration.moved_channels)} 个会话、"
            f"合并 {len(outcome.migration.merged_channels)} 个会话"
            f"{'，账号绑定已过户' if outcome.account_moved else ''}"
        )
    else:
        message = f"已认领实例 '{outcome.target.instance_key}'"

    return InstanceClaimResult(
        ok=not failed,
        instance=await describe_instance(outcome.target),
        source_instance_key=outcome.source_instance_key,
        account_moved=outcome.account_moved,
        source_archived=outcome.source_archived,
        migration=migration,
        message=message,
    )


@router.post("/instances/{instance_key}/claim", summary="认领为新实例")
@require_role(Role.Super)
async def claim_console_instance(
    instance_key: str,
    body: InstanceClaimRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceClaimResult:
    """把一个协议端自行连入、状态为未认领的实例认领下来

    `body.instance_key` 留空时是**就地认领**（只改显示名与状态，一行数据都不动），
    行为与改造前逐字一致。填了就是「用管理员想要的标识接管它」：后端会建一个用该标识的
    实例，再把协议端连同历史整体交给它 —— `instance_key` 编码在全部 `chat_key` 里，
    就地改列会让历史数据一次性变孤儿，所以只能这样表达（详见 `instance_manager`）。

    路径上的实例是**被认领的协议端**，与「并入既有实例」端点（路径是接收方）方向相反：
    两者各自的自然主语不同，前者说"认领它"，后者说"并到我这来"。
    """
    instance = await assert_instance_accessible(_current_user, instance_key)
    outcome = await claim_unclaimed_as_new_instance(instance, body.display_name, body.instance_key)
    return await _build_claim_result(outcome)


@router.post("/instances/{instance_key}/adopt", summary="把待认领协议端并入本实例")
@require_role(Role.Super)
async def adopt_console_instance(
    instance_key: str,
    body: InstanceAdoptRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceClaimResult:
    """把一个未认领的协议端连同它的历史会话并进本实例

    这是现场那个坑的正解：管理员建了 `inst3`（Tsubaki）、接了 napcat，而协议端把自己
    登记成了 `ob{QQ号}` 并拿走了全部频道。走本端点之后，账号绑到 `inst3`、历史频道跟过去、
    `ob{QQ号}` 归档，管理员的心智模型与系统状态重新对齐。

    搬运**直接复用** `chat_key_migration.migrate_instance_ownership()`（预览、幂等、
    逐频道事务与按频道的失败隔离都在那里），本端点只多做两件它做不到的事：把账号绑定过户、
    把交完货的源实例归档。

    需要回填源实例标识作二次确认，与历史归属迁移、彻底删除同一套形态。
    幂等：源实例已被认领（不再是 `unclaimed`）时返回 400，不会重复搬运。
    """
    target = await assert_instance_accessible(_current_user, instance_key)
    source = await assert_instance_accessible(_current_user, body.source_instance_key)
    if body.confirm_source_instance_key != source.instance_key:
        raise ValidationError(reason="并入协议端需要回填正确的待认领实例标识作为二次确认")

    outcome = await adopt_unclaimed_instance(source, target)
    return await _build_claim_result(outcome)


async def _resolve_migration_pair(
    current_user: DBUser,
    target_instance_key: str,
    source_instance_key: str,
) -> Tuple[DBAdapterInstance, DBAdapterInstance]:
    """取出并校验「源实例 → 目标实例」这一对

    路径上的实例是**目标**（"把某个实例的历史并到本实例来"），源实例走请求参数。
    任一端不存在都是 404；配对本身不合法（同一个实例、跨适配器）是 400。
    """
    target = await assert_instance_accessible(current_user, target_instance_key)
    source = await assert_instance_accessible(current_user, source_instance_key)
    if source.id == target.id:
        raise ValidationError(reason="源实例与目标实例不能是同一个")
    if source.adapter_key != target.adapter_key:
        raise ValidationError(reason="只能在同一适配器下的实例之间迁移历史归属")
    return source, target


@router.get("/instances/{instance_key}/migration/preview", summary="预览实例历史归属迁移")
@require_role(Role.Super)
async def preview_console_instance_migration(
    instance_key: str,
    source_instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceMigrationPreview:
    """预览把源实例的历史归属迁到本实例会改动什么

    只读。迁移是破坏性且不可逆的，界面必须先展示本预览再让用户二次确认。

    权限是 `Role.Super`（平台方专属）：跨实例迁移会移动数据归属，租户不能自助把别人的历史
    迁到自己名下。租户在旧权限体系里被钉死在 `Role.Admin`，因此这里对他们恒为 403 ——
    这是**能力边界**而不是资源存在性，与越权访问某个具体实例用 404 是两回事
    （拒绝发生在进入函数体之前，不区分实例标识，因此不泄露任何实例是否存在）。
    """
    source, target = await _resolve_migration_pair(_current_user, instance_key, source_instance_key)
    plan = await plan_instance_migration(target.adapter_key, source.instance_key, target.instance_key)

    warnings: List[str] = []
    if source.provider_account_id and source.provider_account_id == target.provider_account_id:
        warnings.append(
            f"两个实例绑定的是同一个账号 {source.provider_account_id}，"
            "消息路由在这种状态下不确定；迁移完成后请解绑或删除源实例",
        )
    if source.tenant_id != target.tenant_id:
        warnings.append(
            f"源实例归属租户 {source.tenant_id if source.tenant_id is not None else '平台方'}，"
            f"目标实例归属租户 {target.tenant_id if target.tenant_id is not None else '平台方'}；"
            "迁移会把这些会话与记忆一并交给另一方",
        )
    if not plan.channels:
        warnings.append("源实例名下已经没有会话，执行迁移不会改动任何频道")

    return InstanceMigrationPreview(
        adapter_key=plan.adapter_key,
        source_instance_key=plan.source_instance_key,
        source_display_name=source.display_name,
        target_instance_key=plan.target_instance_key,
        target_display_name=target.display_name,
        channel_total=len(plan.channels),
        conflict_total=sum(1 for channel in plan.channels if channel.conflict),
        counts=plan.counts,
        channels=[
            InstanceMigrationChannelItem(
                channel_id=channel.channel_id,
                channel_name=channel.channel_name,
                source_chat_key=channel.source_chat_key,
                target_chat_key=channel.target_chat_key,
                conflict=channel.conflict,
                message_count=channel.message_count,
            )
            for channel in plan.channels
        ],
        residues=[
            InstanceMigrationResidueItem(
                resource=resource,
                count=count,
                reason=INSTANCE_MIGRATION_RESIDUE_REASONS.get(resource, ""),
            )
            for resource, count in plan.residues.items()
            if count
        ],
        warnings=warnings,
    )


@router.post("/instances/{instance_key}/migration", summary="执行实例历史归属迁移")
@require_role(Role.Super)
async def run_console_instance_migration(
    instance_key: str,
    body: InstanceMigrationRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceMigrationResult:
    """把源实例名下的会话、消息、执行记录与记忆改判给本实例

    **破坏性且不可逆**，需要回填源实例标识作二次确认（与彻底删除实例同一套形态）。
    幂等：源实例名下已经没有会话时重复执行是一次空操作。

    权限同预览端点：平台方专属。
    """
    source, target = await _resolve_migration_pair(_current_user, instance_key, body.source_instance_key)
    if body.confirm_source_instance_key != source.instance_key:
        raise ValidationError(reason="迁移历史归属需要回填正确的源实例标识作为二次确认")

    outcome = await migrate_instance_ownership(target.adapter_key, source.instance_key, target.instance_key)
    message = (
        f"已迁移 {len(outcome.moved_channels)} 个会话、合并 {len(outcome.merged_channels)} 个会话"
        f"（{source.instance_key} -> {target.instance_key}）"
    )
    if outcome.failed_channels:
        message += f"；{len(outcome.failed_channels)} 个会话迁移失败仍留在源实例，可重试本操作补齐"

    return InstanceMigrationResult(
        ok=not outcome.failed_channels,
        source_instance_key=outcome.source_instance_key,
        target_instance_key=outcome.target_instance_key,
        moved_channels=outcome.moved_channels,
        merged_channels=outcome.merged_channels,
        failed_channels=outcome.failed_channels,
        counts=outcome.counts,
        residual=outcome.residual,
        message=message,
    )
