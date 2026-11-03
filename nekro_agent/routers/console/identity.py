"""总后台「身份与域」路由：域配置、账号合并、人设孤儿数据

## 本模块最严重的风险面是**跨租户串域**

把别的租户的实例拉进自己的域，等于当场读到对方的记忆与好感。因此每一条端点都要
同时满足两件事：

1. **接口层**：动到实例就走 `assert_instance_accessible`（越权 404，不暴露存在性），
   动到组就走 `_assert_domain_accessible`（同上，按 `get_tenant_scope` 判归属）；
2. **模型层**：域归属的写入**只走** `DBInstanceDomainBinding.bind_to_groups`，
   它自己会再校验一次 `assert_instance_joinable`。

两扇门缺一不可，理由与 #51（模型组写侧归属校验）逐字相同：接口层挡的是"够不着"，
模型层挡的是"从别的入口进来"。本模块**一次都不直接 `create` / `update` 绑定行**。

## 合并的作用域怎么表达

别名表按**域**隔离（`identity_alias.domain_id`），而域不是一个能直接鉴权的对象——
它可能是一个组，也可能是某个实例自成的一域。因此本模块统一用**实例**作为入口：
调用方给一个自己够得着的 `instance_key`，服务端解析出它的域，别名只在那个域里读写。
于是"租户只能合并自己域内的账号"这条约束落在了已有的 `assert_instance_accessible` 上，
不需要为域另写一套归属判定。

撤销同理：`DELETE /identity/aliases/{alias_id}` **必须**带 `instance_key`，
服务端按 `(域, 行 ID)` 双条件删。只按行 ID 删等于让任何一个能进后台的人删掉别的租户的合并。

## 好感域覆盖字段前端不暴露

`DBInstanceDomainBinding.affection_group_id` 存在，但本模块的写入口只填 `memory_group`
（好感域因此跟随记忆域）。这是 2026-08-12 拍板的"默认就绑在一起配一个域"，
那个字段留着只是为了将来不必改表，见 `models/db_instance_group.py` 的模块文档。
"""

from datetime import datetime
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_identity import (
    ALIAS_REQUEST_APPROVED,
    ALIAS_REQUEST_CANCELED,
    ALIAS_REQUEST_PENDING,
    ALIAS_REQUEST_REJECTED,
    DBIdentityAliasRequest,
)
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ActionOkResponse
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services.affection.merge import MergeResult, merge_accounts, unmerge_account
from nekro_agent.services.affection.store import invalidate_cache
from nekro_agent.services.identity_admin import (
    AccountFacts,
    IdentityAdminRejected,
    alias_in_domain,
    clean_name,
    clean_note,
    cleanup_orphan_preset,
    domain_label,
    group_names,
    list_aliases,
    list_domains,
    list_instance_domains,
    list_orphan_buckets,
    merge_self_domain_into_group,
    preview_domain_join,
    preview_merge,
    release_group_members,
)
from nekro_agent.services.identity_requests import (
    AliasRequestError,
    approve_alias_request,
    get_request_in_domain,
    list_requests,
    reject_alias_request,
)
from nekro_agent.services.identity_scope import ScopeDomain, resolve_memory_domain
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    assert_instance_accessible,
    assert_tenant_accessible,
    get_tenant_scope,
)

logger = get_sub_logger("console_api")
router = APIRouter(tags=["Console"])

INSTANCE_SCOPE_DESC = "域的入场券：一个你够得着的实例标识，服务端据此解析出要操作的域"


# ==================== 出参 ====================


class DomainMemberInfo(BaseModel):
    adapter_key: str
    instance_key: str
    display_name: str = ""


class DomainInfo(BaseModel):
    """一个域（实例组）"""

    group_id: int
    name: str
    note: str = ""
    tenant_id: Optional[int] = None
    members: List[DomainMemberInfo] = Field(default_factory=list)
    create_time: Optional[str] = None
    update_time: Optional[str] = None


class DomainJoinMigrationInfo(BaseModel):
    """入域并入的实做结果；只在请求勾选 `migrate_affection` 时随响应返回"""

    affection_rows: int
    folded_rows: int
    score_delta: int
    event_rows: int
    # 其 uid 被目标域别名归一成「最终的人」的源行数
    remapped_rows: int = 0
    alias_rows: int
    alias_conflicts: List[str] = Field(default_factory=list)
    # 迁入时被跟到终点改写了指向的源别名，形如「A（P → Q）」
    alias_rewrites: List[str] = Field(default_factory=list)
    person_rows: int
    person_conflicts: List[str] = Field(default_factory=list)


class InstanceDomainItem(BaseModel):
    """一个实例当前在哪个域；`group_id` 为空表示自成一域（= 不配置时的行为）"""

    adapter_key: str
    instance_key: str
    display_name: str = ""
    tenant_id: Optional[int] = None
    group_id: Optional[int] = None
    group_name: str = ""
    domain_id: str
    migration: Optional[DomainJoinMigrationInfo] = None


class DomainJoinPreviewResponse(BaseModel):
    """「实例入域并入存量好感」的预览——点之前先看清楚影响面"""

    adapter_key: str
    instance_key: str
    source_domain_id: str
    target_domain_id: str
    target_domain_label: str
    affection_rows: int
    affection_people: int
    event_rows: int
    fold_pairs: int
    remapped_rows: int = 0
    alias_rows: int
    alias_conflicts: List[str] = Field(default_factory=list)
    alias_rewrites: List[str] = Field(default_factory=list)
    person_rows: int
    blockers: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    can_migrate: bool = True


class DomainListResponse(BaseModel):
    items: List[DomainInfo]
    instances: List[InstanceDomainItem]
    # 平台方才有"给别的租户建组"这档；租户身份下恒为 False，前端据此不画那个选择器
    can_choose_tenant: bool = False


class DomainCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="组名称，仅用于展示，不要求唯一")
    note: str = Field(default="", description="备注")
    tenant_id: Optional[int] = Field(default=None, description="归属租户；仅平台方可指定，租户身份下忽略")


class DomainUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, description="组名称")
    note: Optional[str] = Field(default=None, description="备注")


class InstanceDomainUpdateRequest(BaseModel):
    """把实例放进（或移出）某个域

    `group_id` 传 null 表示回到**自成一域**——绑定行会被删掉，
    "没有行"才是零配置的标准形态。

    `migrate_affection` 是入域并入开关：勾选后把该实例**自成一域时期**的存量好感
    一并搬进目标组域（撞上的同（人, 人设）行按「取高」折叠）。只在
    「自成一域 → 组域」这一个方向上合法，其余方向传 true 会被拒——
    组域里的数据是全体成员的共享历史，随实例搬走会把别家成员的数据带走。
    """

    group_id: Optional[int] = Field(default=None, description="目标实例组 ID；null 表示自成一域")
    migrate_affection: bool = Field(default=False, description="并入该实例自成一域时期的存量好感")


class PresetAffectionItem(BaseModel):
    preset_id: int
    preset_name: str
    preset_exists: bool
    score: int
    stage: str
    stage_label: str
    interaction_count: int
    last_interact_at: Optional[str] = None


class AccountFactsItem(BaseModel):
    platform_userid: str
    nickname: str = ""
    alias_person_uid: str = ""
    incoming_alias_count: int = 0
    best_score: int = 0
    affection_rows: int = 0
    event_rows: int = 0
    interaction_total: int = 0
    last_interact_at: Optional[str] = None
    presets: List[PresetAffectionItem] = Field(default_factory=list)


class MergeTransferItem(BaseModel):
    """某个人设下「将采用哪个值」"""

    preset_id: int
    preset_name: str
    preset_exists: bool
    alias_score: int
    person_score: int
    delta: int
    score_after: int


class MergePreviewResponse(BaseModel):
    domain_id: str
    domain_label: str
    alias: AccountFactsItem
    person: AccountFactsItem
    transfers: List[MergeTransferItem] = Field(default_factory=list)
    total_delta: int = 0
    domain_memory_paragraphs: int
    memory_scope_note: str
    reversible_note: str
    warnings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    can_merge: bool = True


class MergeRequest(BaseModel):
    instance_key: str = Field(..., description=INSTANCE_SCOPE_DESC)
    alias_userid: str = Field(..., min_length=1, description="副账号；合并后它指向主账号")
    person_userid: str = Field(..., min_length=1, description="主账号；这个人的标识就是它")
    note: str = Field(default="", description="备注，会记进别名行")


class MergeTransferResultItem(BaseModel):
    """一次合并/撤销在某个人设上实际补（扣）了多少"""

    preset_id: int
    delta: int
    score_after: int


class MergeExecuteResponse(BaseModel):
    """合并/撤销的执行结果，如实回显 `services/affection/merge.py` 返回的那一份"""

    domain_id: str
    person_uid: str
    alias_platform_userid: str
    total_delta: int
    transfers: List[MergeTransferResultItem] = Field(default_factory=list)
    # 因为「别名不成链」被一并改写的其他副号；撤销时它们**不会**自动跟回去
    rewritten_aliases: List[str] = Field(default_factory=list)


class AliasItem(BaseModel):
    id: int
    domain_id: str
    platform_userid: str
    person_uid: str
    source: str
    operator: str = ""
    note: str = ""
    create_time: Optional[str] = None


class AliasListResponse(BaseModel):
    domain_id: str
    domain_label: str
    items: List[AliasItem]


class AliasRequestItem(BaseModel):
    """一张自助申报单；`reporter_*` 两列是给管理员核对"谁报的"用的，不是解析结果"""

    id: int
    alias_platform_userid: str
    reporter_userid: str
    reporter_person_uid: str
    note: str = ""
    status: str
    reviewed_by: str = ""
    review_note: str = ""
    reviewed_at: Optional[str] = None
    create_time: Optional[str] = None


class AliasRequestListResponse(BaseModel):
    domain_id: str
    domain_label: str
    items: List[AliasRequestItem]


class AliasRequestReviewBody(BaseModel):
    instance_key: str = Field(..., description=INSTANCE_SCOPE_DESC)
    note: str = Field(default="", description="审批备注（批准时落进别名行；拒绝时回给申报人）")


class AliasRequestApproveResponse(BaseModel):
    """批准的回执：申报单终态 + 合并的实做结果（与手动合并同一个形状）"""

    request: AliasRequestItem
    merge: MergeExecuteResponse


class OrphanBucketItem(BaseModel):
    preset_id: int
    domain_id: str
    domain_label: str
    adapter_key: str
    instance_key: str
    domain_group_id: int
    affection_rows: int
    event_rows: int
    people: int
    last_update: Optional[str] = None


class OrphanListResponse(BaseModel):
    items: List[OrphanBucketItem]
    # 人设删除**不级联删**数据；这句话跟着响应走，免得前端各写一份措辞
    policy_note: str


class OrphanCleanupRequest(BaseModel):
    instance_key: str = Field(..., description=INSTANCE_SCOPE_DESC)
    preset_id: int = Field(..., gt=0, description="要清理的已删除人设 ID")
    # 一个实例在孤儿清单里可以合法地有两种桶（进组前留在自成一域上的、进组后落在组域上的），
    # 所以这一维是**选哪一桶**，不是"再确认一次实例在哪个域"。校验方式见端点 docstring。
    domain_id: str = Field(..., description="要清理的那一桶的域标识；必须是该实例孤儿清单里现存的一桶")


class OrphanCleanupResponse(BaseModel):
    ok: bool = True
    affection: int = 0
    affection_event: int = 0


ORPHAN_POLICY_NOTE = (
    "人设被删除后，挂在它下面的好感与流水原地保留成孤儿，不级联删除"
    "（人设可能是误删，云端人设重新导入会带回同一个 ID）。这里列出的每一桶都要管理员手动清理。"
)


# ==================== 作用域守卫 ====================


async def _assert_domain_accessible(user: DBUser, group_id: int) -> DBInstanceGroup:
    """校验当前身份能否操作某个域（实例组），越权一律 404

    判据与 `assert_instance_accessible` 同源：不存在与无权访问对外不可区分，
    否则租户能拿组 ID 枚举出平台方建了几个域。
    """
    scope = get_tenant_scope(user)
    group = await DBInstanceGroup.get_or_none(id=group_id)
    if group is None or (scope is not None and group.tenant_id != scope):
        raise NotFoundError(resource=f"实例组 '{group_id}'")
    return group


async def _resolve_domain(user: DBUser, instance_key: str) -> Tuple[DBAdapterInstance, ScopeDomain]:
    """把"一个够得着的实例"换成"要操作的域"

    这是本模块表达作用域的唯一方式：`assert_instance_accessible` 挡住越权（404），
    域再由 `resolve_memory_domain` 从那个实例解析出来。**不要**让调用方直接传 `domain_id`
    ——那是一个没有归属的字符串，谁都能编一个。
    """
    key = instance_key.strip()
    if not key:
        raise ValidationError(reason="必须指定实例作用域")
    instance = await assert_instance_accessible(user, key)
    domain = await resolve_memory_domain(instance.adapter_key, instance.instance_key)
    return instance, domain


async def _domain_label_of(domain: ScopeDomain) -> str:
    return domain_label(domain, (await group_names([domain.group_id])).get(domain.group_id, ""))


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


# ==================== 域（实例组） ====================


@router.get("/identity/domains", summary="获取记忆域（实例组）清单")
@require_role(Role.Admin)
async def get_identity_domains(
    _current_user: DBUser = Depends(get_current_active_user),
) -> DomainListResponse:
    """列出可见的域，以及每个可见实例当前在哪个域

    平台方看全部；租户只看自己名下的组与实例（`get_tenant_scope` 收敛）。
    """
    scope = get_tenant_scope(_current_user)
    domains = await list_domains(scope)
    instances = await list_instance_domains(scope)
    return DomainListResponse(
        items=[
            DomainInfo(
                group_id=item.group_id,
                name=item.name,
                note=item.note,
                tenant_id=item.tenant_id,
                members=[
                    DomainMemberInfo(
                        adapter_key=member.adapter_key,
                        instance_key=member.instance_key,
                        display_name=member.display_name,
                    )
                    for member in item.members
                ],
                create_time=_iso(item.create_time),
                update_time=_iso(item.update_time),
            )
            for item in domains
        ],
        instances=[
            InstanceDomainItem(
                adapter_key=item.adapter_key,
                instance_key=item.instance_key,
                display_name=item.display_name,
                tenant_id=item.tenant_id,
                group_id=item.group_id,
                group_name=item.group_name,
                domain_id=item.domain_id,
            )
            for item in instances
        ],
        can_choose_tenant=scope is None,
    )


@router.post("/identity/domains", summary="新建记忆域")
@require_role(Role.Admin)
async def create_identity_domain(
    body: DomainCreateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> DomainInfo:
    """新建一个域

    归属由**身份**决定，不由请求体决定：租户身份下 `tenant_id` 一律改写成自己的租户，
    传别人的直接忽略。平台方可以显式指定归属（要给某个客户建组时用），
    但那个租户必须真的存在——`assert_tenant_accessible` 顺带把这件事查了。

    组的归属决定了**哪些实例能加进来**（`assert_instance_joinable` 要求两者逐字相等），
    所以建组这一步就是跨租户串域的第一道闸。
    """
    scope = get_tenant_scope(_current_user)
    tenant_id = scope
    if scope is None and body.tenant_id is not None:
        tenant_id = (await assert_tenant_accessible(_current_user, body.tenant_id)).id

    name = clean_name(body.name).strip()
    if not name:
        raise ValidationError(reason="组名称不能为空")
    group = await DBInstanceGroup.create(name=name, note=clean_note(body.note), tenant_id=tenant_id)
    logger.info(f"新建实例组: id={group.id}, name={group.name}, tenant_id={tenant_id}, by={_current_user.username}")
    return DomainInfo(
        group_id=group.id,
        name=group.name,
        note=group.note,
        tenant_id=group.tenant_id,
        members=[],
        create_time=_iso(group.create_time),
        update_time=_iso(group.update_time),
    )


@router.patch("/identity/domains/{group_id}", summary="修改记忆域")
@require_role(Role.Admin)
async def update_identity_domain(
    group_id: int,
    body: DomainUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> DomainInfo:
    """改组名与备注

    **归属不可改**：改归属等于把一整组数据连人带货搬到另一个租户名下，
    而成员实例的归属并不会跟着变，下一次 `assert_instance_joinable` 就会与现状矛盾。
    要换归属只能删组重建。
    """
    group = await _assert_domain_accessible(_current_user, group_id)
    if body.name is not None:
        name = clean_name(body.name).strip()
        if not name:
            raise ValidationError(reason="组名称不能为空")
        group.name = name
    if body.note is not None:
        group.note = clean_note(body.note)
    await group.save()
    members = await DBInstanceDomainBinding.members_of_memory_group(group.id)
    return DomainInfo(
        group_id=group.id,
        name=group.name,
        note=group.note,
        tenant_id=group.tenant_id,
        members=[
            DomainMemberInfo(adapter_key=adapter_key, instance_key=instance_key)
            for adapter_key, instance_key in members
        ],
        create_time=_iso(group.create_time),
        update_time=_iso(group.update_time),
    )


@router.delete("/identity/domains/{group_id}", summary="删除记忆域")
@require_role(Role.Admin)
async def delete_identity_domain(
    group_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """删除一个域，成员实例回到自成一域

    **必须先释放成员**：`resolve_memory_domain` 刻意不查 `instance_group`，
    绑定行指向一个已被删除的组时仍然按那个组 ID 成立（否则删组会让整组的数据当场"消失"）。
    所以留着绑定行的后果不是"回到默认"，而是"一个后台再也看不见、成员之间却仍然互通的域"。
    """
    group = await _assert_domain_accessible(_current_user, group_id)
    released = await release_group_members(group.id, reason=f"实例组 {group.id} 被 {_current_user.username} 删除")
    await group.delete()
    logger.info(f"删除实例组: id={group_id}, released={released}, by={_current_user.username}")
    return ActionOkResponse(ok=True, message=f"已删除域，{released} 个实例回到自成一域")


@router.put("/identity/instances/{instance_key}/domain", summary="设置实例的记忆域")
@require_role(Role.Admin)
async def set_instance_domain(
    instance_key: str,
    body: InstanceDomainUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceDomainItem:
    """把实例编进某个域，或让它回到自成一域

    两道门缺一不可：

    1. `assert_instance_accessible` —— 这个实例是不是你的（不是就 404）；
    2. `_assert_domain_accessible` —— 这个组是不是你的（不是就 404）；

    最后的写入走 `DBInstanceDomainBinding.bind_to_groups`，它会在模型层再校验一次
    "实例与组的归属逐字相等"。三道加起来才挡得住跨租户串域：前两道挡住"从这个接口进来"，
    第三道挡住"从别的入口进来"。

    **只写 `memory_group`**：好感域跟随记忆域，那个覆盖字段前端不暴露。

    `migrate_affection` 为 true 时把**自成一域时期**的存量好感并入目标组域
    （`merge_self_domain_into_group`，与绑定写入同事务：并入失败则绑定一起回滚，
    不留"绑了域但数据没跟过去"的半成品）。只在「自成一域 → 组域」这一个方向合法：
    组域里的行是全体成员的共享历史，反向并入会把别家成员的数据带走。
    """
    scope = get_tenant_scope(_current_user)
    instance = await assert_instance_accessible(_current_user, instance_key)
    group: Optional[DBInstanceGroup] = None
    if body.group_id is not None:
        group = await _assert_domain_accessible(_current_user, body.group_id)

    if body.migrate_affection:
        if group is None:
            raise ValidationError(reason="「并入存量好感」只在加入实例组时适用")
        current_domain = await resolve_memory_domain(instance.adapter_key, instance.instance_key)
        if current_domain.is_group:
            raise ValidationError(
                reason=f"该实例当前在域「{current_domain.domain_id}」中：存量好感属于组域的共享数据，"
                "不随实例搬走。请先退回自成一域，再加入目标组时勾选并入。",
            )

    try:
        async with in_transaction():
            await DBInstanceDomainBinding.bind_to_groups(
                instance.adapter_key,
                instance.instance_key,
                memory_group=group,
            )
            migration = (
                await merge_self_domain_into_group(
                    adapter_key=instance.adapter_key,
                    instance_key=instance.instance_key,
                    group=group,
                    operator=_current_user.username,
                    # 缓存失效必须在外层事务提交之后（见下），这里不在事务里失效
                    defer_cache_invalidation=True,
                )
                if body.migrate_affection and group is not None
                else None
            )
    except ValueError as error:
        # 模型层的归属校验。走到这里说明接口层的两道门与实例/组的实际归属对不上。
        #
        # **租户一侧按 404**：他的两道门都要求归属等于自己的 `tenant_id`，正常路径下
        # 两者必然相等，能走到这里只可能是并发（刚校验完实例就被划给了别的租户）——
        # 那与"这个实例不是你的"是同一件事，必须对外不可区分。
        #
        # **平台方一侧按 400，并如实说明**：他看得见全部实例与全部组，这里没有任何
        # 存在性要掩盖；回一句"实例不存在"是在对着一个他正盯着的行说谎，而真实原因
        # （把 A 租户的实例放进 B 租户的组）是他一眼就能改的。
        logger.warning(f"实例入域被模型层拒绝: instance={instance_key}, group={body.group_id}, error={error}")
        if scope is None:
            raise ValidationError(reason=str(error)) from error
        raise NotFoundError(resource=f"实例 '{instance_key}'") from error

    # 事务已提交：现在才失效快照缓存。并入搬进来 / 折叠改写的行在提交之前对别的连接不可见，
    # 提交前失效会让并发读者把旧值（或目标键上的「从未打过交道」空快照）重新缓存一个 TTL（P2-6）。
    # 整表清空而非逐键：并入可能同时动了很多键，且 `apply_delta` 内部的逐键失效也发生在事务内。
    # 补偿也改了目标域的行（`apply_delta` 在事务内的逐键失效同样发生在提交之前），只有补偿的并入也要失效（P2-3）
    if migration is not None and (migration.affection_rows or migration.folded_rows or migration.alias_compensated):
        invalidate_cache()

    domain = await resolve_memory_domain(instance.adapter_key, instance.instance_key)
    logger.info(
        f"实例域归属已更新: instance={instance_key}, group_id={body.group_id}, "
        f"migrate_affection={body.migrate_affection}, by={_current_user.username}",
    )
    return InstanceDomainItem(
        adapter_key=instance.adapter_key,
        instance_key=instance.instance_key,
        display_name=instance.display_name,
        tenant_id=instance.tenant_id,
        group_id=group.id if group is not None else None,
        group_name=group.name if group is not None else "",
        domain_id=domain.domain_id,
        migration=(
            DomainJoinMigrationInfo(
                affection_rows=migration.affection_rows,
                folded_rows=migration.folded_rows,
                score_delta=migration.score_delta,
                event_rows=migration.event_rows,
                remapped_rows=migration.remapped_rows,
                alias_rows=migration.alias_rows,
                alias_conflicts=migration.alias_conflicts,
                alias_rewrites=migration.alias_rewrites,
                person_rows=migration.person_rows,
                person_conflicts=migration.person_conflicts,
            )
            if migration is not None
            else None
        ),
    )


@router.get("/identity/instances/{instance_key}/domain-join-preview", summary="实例入域并入存量好感预览")
@require_role(Role.Admin)
async def get_domain_join_preview(
    instance_key: str,
    group_id: int = Query(..., description="目标实例组 ID"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> DomainJoinPreviewResponse:
    """入域并入的影响面：会搬多少行、有多少会折叠、哪些别名要以目标域为准

    与 `set_instance_domain` 同一组守卫（实例归属 + 组归属），只是把"会写什么"
    先算给操作者看。`blockers` 非空时前端置灰「并入」选项——真正的判据在执行侧，
    这里只是提前说清楚。
    """
    instance = await assert_instance_accessible(_current_user, instance_key)
    group = await _assert_domain_accessible(_current_user, group_id)
    preview = await preview_domain_join(
        adapter_key=instance.adapter_key,
        instance_key=instance.instance_key,
        group=group,
    )
    return DomainJoinPreviewResponse(
        adapter_key=preview.adapter_key,
        instance_key=preview.instance_key,
        source_domain_id=preview.source_domain_id,
        target_domain_id=preview.target_domain_id,
        target_domain_label=preview.target_domain_label,
        affection_rows=preview.affection_rows,
        affection_people=preview.affection_people,
        event_rows=preview.event_rows,
        fold_pairs=preview.fold_pairs,
        remapped_rows=preview.remapped_rows,
        alias_rows=preview.alias_rows,
        alias_conflicts=preview.alias_conflicts,
        alias_rewrites=preview.alias_rewrites,
        person_rows=preview.person_rows,
        blockers=preview.blockers,
        notes=preview.notes,
        can_migrate=preview.can_migrate,
    )


# ==================== 账号合并 ====================


@router.get("/identity/aliases", summary="获取域内已生效的账号合并")
@require_role(Role.Admin)
async def get_identity_aliases(
    instance_key: str = Query(..., description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AliasListResponse:
    """列出这个域里所有"这两个号是同一个人"的记录"""
    _instance, domain = await _resolve_domain(_current_user, instance_key)
    return AliasListResponse(
        domain_id=domain.domain_id,
        domain_label=await _domain_label_of(domain),
        items=[
            AliasItem(
                id=item.id,
                domain_id=item.domain_id,
                platform_userid=item.platform_userid,
                person_uid=item.person_uid,
                source=item.source,
                operator=item.operator,
                note=item.note,
                create_time=_iso(item.create_time),
            )
            for item in await list_aliases(domain)
        ],
    )


@router.post("/identity/merge/preview", summary="账号合并预览")
@require_role(Role.Admin)
async def preview_identity_merge(
    body: MergeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MergePreviewResponse:
    """先预览后执行的那个"先"

    回两边各自的好感值、逐人设明细、流水条数，以及**每个人设合完之后采用哪个值**。
    「好感取高的」由 `services/affection/merge.py` 的补偿事件实现，因此
    `transfers[].score_after` 就是那个"将采用的值"，`delta` 是这次要补给主账号的差额。
    """
    instance, domain = await _resolve_domain(_current_user, body.instance_key)
    preview = await preview_merge(
        domain,
        adapter_key=instance.adapter_key,
        instance_key=instance.instance_key,
        alias_userid=body.alias_userid.strip(),
        person_userid=body.person_userid.strip(),
    )
    return MergePreviewResponse(
        domain_id=preview.domain_id,
        domain_label=preview.domain_label,
        alias=_to_account(preview.alias),
        person=_to_account(preview.person),
        transfers=[
            MergeTransferItem(
                preset_id=transfer.preset_id,
                preset_name=transfer.preset_name,
                preset_exists=transfer.preset_exists,
                alias_score=transfer.alias_score,
                person_score=transfer.person_score,
                delta=transfer.delta,
                score_after=transfer.score_after,
            )
            for transfer in preview.transfers
        ],
        total_delta=preview.total_delta,
        domain_memory_paragraphs=preview.domain_memory_paragraphs,
        memory_scope_note=preview.memory_scope_note,
        reversible_note=preview.reversible_note,
        warnings=preview.warnings,
        blockers=preview.blockers,
        can_merge=preview.can_merge,
    )


def _to_merge_response(result: MergeResult) -> MergeExecuteResponse:
    """把 `services/affection/merge.py` 的结果原样转成出参，合并与撤销共用一份"""
    return MergeExecuteResponse(
        domain_id=result.domain_id,
        person_uid=result.person_uid,
        alias_platform_userid=result.alias_platform_userid,
        total_delta=result.total_delta,
        transfers=[
            MergeTransferResultItem(preset_id=item.preset_id, delta=item.delta, score_after=item.score_after)
            for item in result.transfers
        ],
        rewritten_aliases=list(result.rewritten_aliases),
    )


def _to_account(facts: AccountFacts) -> AccountFactsItem:
    return AccountFactsItem(
        platform_userid=facts.platform_userid,
        nickname=facts.nickname,
        alias_person_uid=facts.alias_person_uid,
        incoming_alias_count=facts.incoming_alias_count,
        best_score=facts.best_score,
        affection_rows=facts.affection_rows,
        event_rows=facts.event_rows,
        interaction_total=facts.interaction_total,
        last_interact_at=_iso(facts.last_interact_at),
        presets=[
            PresetAffectionItem(
                preset_id=row.preset_id,
                preset_name=row.preset_name,
                preset_exists=row.preset_exists,
                score=row.score,
                stage=row.stage,
                stage_label=row.stage_label,
                interaction_count=row.interaction_count,
                last_interact_at=_iso(row.last_interact_at),
            )
            for row in facts.presets
        ],
    )


@router.post("/identity/merge", summary="执行账号合并")
@require_role(Role.Admin)
async def execute_identity_merge(
    body: MergeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MergeExecuteResponse:
    """把副账号并进主账号

    执行本身**不在本模块**：别名行、逐人设的「好感取高的」补偿事件、以及「别名不成链」的
    改写，全部由 `services/affection/merge.py:merge_accounts` 负责。本端点只做两件事——
    把作用域解析成一个域（越权 404），把那个服务的拒绝理由转成 400。

    合错了走 `DELETE /identity/aliases/{id}` 撤销：删别名 + 把补进去的那一份原样扣回，
    副账号自己那一行一个字节都没动过。
    """
    instance, domain = await _resolve_domain(_current_user, body.instance_key)
    try:
        result = await merge_accounts(
            domain,
            person_uid=body.person_userid,
            alias_platform_userid=body.alias_userid,
            operator=_current_user.username,
            note=clean_note(body.note),
        )
    except ValueError as error:
        raise ValidationError(reason=str(error)) from error

    logger.info(
        f"账号合并: domain={domain.domain_id}, instance={instance.instance_key}, "
        f"{result.alias_platform_userid} -> {result.person_uid}, 补偿 {result.total_delta} 点, "
        f"by={_current_user.username}",
    )
    return _to_merge_response(result)


@router.delete("/identity/aliases/{alias_id}", summary="撤销账号合并")
@require_role(Role.Admin)
async def revoke_identity_merge(
    alias_id: int,
    instance_key: str = Query(..., description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> MergeExecuteResponse:
    """撤销一次合并

    `instance_key` **不是**可选的：别名表是全局一张，只按行 ID 删等于让任何一个
    能进后台的人撤销别的租户的合并。这里先用 `alias_in_domain` 按 `(域, 行 ID)` 双条件
    定位，域里没有那一行就 404——与"这一行不存在"对外不可区分——再把真正的撤销
    （删别名 + 扣回补偿）交给 `services/affection/merge.py:unmerge_account`。
    """
    _instance, domain = await _resolve_domain(_current_user, instance_key)
    target = await alias_in_domain(domain, alias_id)
    if target is None:
        raise NotFoundError(resource=f"账号合并记录 '{alias_id}'")
    try:
        result = await unmerge_account(
            domain,
            alias_platform_userid=target.platform_userid,
            operator=_current_user.username,
        )
    except ValueError as error:
        raise ValidationError(reason=str(error)) from error

    logger.info(
        f"账号合并已撤销: domain={domain.domain_id}, alias={target.platform_userid}, "
        f"扣回 {result.total_delta} 点, by={_current_user.username}",
    )
    return _to_merge_response(result)


# ==================== 自助申报审批 ====================

_ALIAS_REQUEST_STATUSES = (
    ALIAS_REQUEST_PENDING,
    ALIAS_REQUEST_APPROVED,
    ALIAS_REQUEST_REJECTED,
    ALIAS_REQUEST_CANCELED,
)


def _to_request_item(request: DBIdentityAliasRequest) -> AliasRequestItem:
    return AliasRequestItem(
        id=request.id,
        alias_platform_userid=request.alias_platform_userid,
        reporter_userid=request.reporter_userid,
        reporter_person_uid=request.reporter_person_uid,
        note=request.note,
        status=request.status,
        reviewed_by=request.reviewed_by,
        review_note=request.review_note,
        reviewed_at=_iso(request.reviewed_at),
        create_time=_iso(request.create_time),
    )


async def _resolve_request(user: DBUser, instance_key: str, request_id: int) -> Tuple[ScopeDomain, DBIdentityAliasRequest]:
    """按 `(域, 单号)` 双条件定位一张申报单，域里没有就 404

    与 `revoke_identity_merge` 的 `alias_in_domain` 同一条理由：单号是全局自增的，
    只按单号审批等于让任何一个能进某域后台的人批掉别的域的单子。
    """
    _instance, domain = await _resolve_domain(user, instance_key)
    try:
        request = await get_request_in_domain(domain, request_id)
    except AliasRequestError as error:
        raise NotFoundError(resource=f"账号合并申报 '{request_id}'") from error
    return domain, request


@router.get("/identity/alias-requests", summary="获取账号合并自助申报列表")
@require_role(Role.Admin)
async def list_identity_alias_requests(
    instance_key: str = Query(..., description=INSTANCE_SCOPE_DESC),
    status: str = Query(default=ALIAS_REQUEST_PENDING, description="按状态过滤；传空串列全部"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AliasRequestListResponse:
    """列出这个域里的自助申报单，默认只看待审批"""
    _instance, domain = await _resolve_domain(_current_user, instance_key)
    wanted = status.strip()
    if wanted and wanted not in _ALIAS_REQUEST_STATUSES:
        raise ValidationError(reason=f"非法的申报状态: {wanted}")
    return AliasRequestListResponse(
        domain_id=domain.domain_id,
        domain_label=await _domain_label_of(domain),
        items=[
            _to_request_item(item)
            for item in await list_requests(domain, status=wanted or None)
        ],
    )


@router.post("/identity/alias-requests/{request_id}/approve", summary="批准账号合并申报")
@require_role(Role.Admin)
async def approve_identity_alias_request(
    request_id: int,
    body: AliasRequestReviewBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AliasRequestApproveResponse:
    """批准 = 兑现成一条 `source=self_report` 的别名

    与手动合并**完全同一条路径**（`merge_accounts` 的不成链/已合并校验一条不少），
    本端点只在它前面加两道申报单自己的事：单子按 `(域, 单号)` 定位、
    申报人的「人」标识中途漂移则拒（服务层复核）。审批结果回申报人当时的会话。
    """
    domain, _request = await _resolve_request(_current_user, body.instance_key, request_id)
    try:
        request, result = await approve_alias_request(
            domain,
            request_id,
            operator=_current_user.username,
            note=clean_note(body.note),
        )
    except AliasRequestError as error:
        raise ValidationError(reason=str(error)) from error

    logger.info(
        f"批准账号合并申报: domain={domain.domain_id}, request={request_id}, "
        f"{result.alias_platform_userid} -> {result.person_uid}, by={_current_user.username}",
    )
    return AliasRequestApproveResponse(request=_to_request_item(request), merge=_to_merge_response(result))


@router.post("/identity/alias-requests/{request_id}/reject", summary="拒绝账号合并申报")
@require_role(Role.Admin)
async def reject_identity_alias_request(
    request_id: int,
    body: AliasRequestReviewBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AliasRequestItem:
    """拒绝 = 只留痕：状态、批语、谁批的、什么时候。永远不动别名表。"""
    domain, _request = await _resolve_request(_current_user, body.instance_key, request_id)
    try:
        request = await reject_alias_request(
            domain,
            request_id,
            operator=_current_user.username,
            note=clean_note(body.note),
        )
    except AliasRequestError as error:
        raise ValidationError(reason=str(error)) from error

    logger.info(f"拒绝账号合并申报: domain={domain.domain_id}, request={request_id}, by={_current_user.username}")
    return _to_request_item(request)


# ==================== 人设孤儿数据 ====================


@router.get("/identity/orphans", summary="获取人设孤儿数据")
@require_role(Role.Admin)
async def get_identity_orphans(
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC + "；平台方留空表示全局视图"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> OrphanListResponse:
    """列出挂在**已删除人设**下的好感数据

    作用域与 `routers/affection.py` 同一形态：平台方留空 = 全局视图，
    租户必须指定自己名下的实例（留空对他们不能退化成全局）。
    """
    scope = get_tenant_scope(_current_user)
    key = instance_key.strip()
    if scope is not None and not key:
        raise ValidationError(reason="租户身份必须指定实例作用域")
    instances: Optional[List[Tuple[str, str]]] = None
    if key:
        instance = await assert_instance_accessible(_current_user, key)
        instances = [(instance.adapter_key, instance.instance_key)]

    buckets = await list_orphan_buckets(instances)
    return OrphanListResponse(
        items=[
            OrphanBucketItem(
                preset_id=item.preset_id,
                domain_id=item.domain_id,
                domain_label=item.domain_label,
                adapter_key=item.adapter_key,
                instance_key=item.instance_key,
                domain_group_id=item.domain_group_id,
                affection_rows=item.affection_rows,
                event_rows=item.event_rows,
                people=item.people,
                last_update=_iso(item.last_update),
            )
            for item in buckets
        ],
        policy_note=ORPHAN_POLICY_NOTE,
    )


@router.post("/identity/orphans/cleanup", summary="清理一桶人设孤儿数据")
@require_role(Role.Admin)
async def cleanup_identity_orphans(
    body: OrphanCleanupRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> OrphanCleanupResponse:
    """手动清理一桶孤儿数据

    **一次只清一桶**，而且那一桶必须出现在**此刻重新解析出来的**清单里
    （`list_orphan_buckets([这个实例])`）。这条既是授权也是防陈旧：清单本身只收
    "这个实例自己的行"与"它当前所在的组的行"，两者都在调用者够得着的范围内；
    预览与执行之间实例被移出某个组时，那个组的桶下一刻就不在清单里了，于是 404。

    **判据不能写成"请求里的 `domain_id` 等于本实例此刻的域"**，那是本模块曾经的写法，
    它与 `list_orphan_buckets` 自相矛盾：一个实例在清单里合法地有**两种**桶——
    进组之前留在自成一域上的（`i:适配器:实例`）与进组之后落在组域上的（`g:组ID`）。
    只认后者会让前者永远清不掉：清单里看得见、按钮永远报"域已经变了"、刷新之后还是它。
    而"进组之前留下的那些行"恰恰是**存量数据**里最需要能清掉的一类。
    """
    instance, domain = await _resolve_domain(_current_user, body.instance_key)
    buckets = [
        bucket
        for bucket in await list_orphan_buckets([(instance.adapter_key, instance.instance_key)])
        if bucket.preset_id == body.preset_id and bucket.domain_id == body.domain_id
    ]
    if not buckets:
        # 与"这一桶不存在"对外不可区分：越权、桶已被清掉、以及"实例已经移出那个组"
        # 三种情况都走这里，理由与 `assert_instance_accessible` 的存在性掩盖同源。
        raise NotFoundError(resource=f"人设 '{body.preset_id}' 在域 '{body.domain_id}' 下的孤儿数据")

    try:
        counts = await cleanup_orphan_preset(buckets[0])
    except IdentityAdminRejected as error:
        raise ValidationError(reason=str(error)) from error

    logger.warning(
        f"孤儿数据清理: domain={buckets[0].domain_id}, 实例当前域={domain.domain_id}, "
        f"preset_id={body.preset_id}, counts={counts}, by={_current_user.username}",
    )
    return OrphanCleanupResponse(
        ok=True,
        affection=counts["affection"],
        affection_event=counts["affection_event"],
    )
