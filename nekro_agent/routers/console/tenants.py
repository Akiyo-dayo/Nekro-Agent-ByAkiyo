"""总后台租户管理路由

## 权限分层

- **平台方专属**：租户的增删改查、以及"把实例划归某租户"。实例归属是商业关系的落点，
  租户管理员绝不能自助把别人的实例划到自己名下，因此 `PUT /instances` 是平台方专属。
- **平台方或租户管理员**：成员的增删。租户管理员可以自助管理自己团队的账号。
- **任意已登录身份（作用域受限）**：读取租户详情与成员列表。

所有带 `{tenant_id}` 的端点都先过 `assert_tenant_accessible`，租户访问不属于自己的
租户一律得到 **404** 而非 403 —— 403 会把"这个租户 ID 存在"泄露出去，
让平台方的客户清单变成可枚举的。

## 为什么业务逻辑写在路由层

租户的读写逻辑本应落在 `services/console/`，但该目录由并行开发的其他模块占用。
本模块暂时自包含，待并行开发合流后可整体下沉，届时路由只保留参数校验与依赖声明。
"""

import re
from typing import Dict, List, Tuple

from fastapi import APIRouter, Depends
from tortoise.functions import Count
from tortoise.timezone import now as db_now

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ActionOkResponse
from nekro_agent.schemas.errors import ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.tenant import (
    TenantCreateRequest,
    TenantInfo,
    TenantInstanceBindRequest,
    TenantListResponse,
    TenantMemberCreateRequest,
    TenantMemberInfo,
    TenantRole,
    TenantStatus,
    TenantUpdateRequest,
)
from nekro_agent.services.user.auth import get_hashed_password
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.tenant_perm import (
    assert_tenant_accessible,
    is_platform_owner,
    require_platform_owner,
    require_tenant_admin,
    tenant_perm_level,
    tenant_role_of,
)
from nekro_agent.services.user.token_exposure import record_instances_exposure

logger = get_sub_logger("console.tenant")

router = APIRouter(tags=["Console"])

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 租户标识用于人读与日志定位，约束成 slug 以免出现空格、大小写混淆与路径歧义
TENANT_NAME_PATTERN = re.compile(r"^[a-z0-9_]{2,64}$")

# 平台方唯一的后台账号，永远不属于任何租户，成员创建时必须避开
RESERVED_USERNAME = "admin"


async def count_by_tenant() -> Tuple[Dict[int, int], Dict[int, int]]:
    """一次性聚合出每个租户的实例数与成员数

    固定两条 group by 查询，与租户数量无关，避免列表接口退化成 N+1。
    """
    instance_rows = (
        await DBAdapterInstance.filter(tenant_id__isnull=False)
        .annotate(cnt=Count("id"))
        .group_by("tenant_id")
        .values("tenant_id", "cnt")
    )
    member_rows = (
        await DBUser.filter(tenant_id__isnull=False).annotate(cnt=Count("id")).group_by("tenant_id").values("tenant_id", "cnt")
    )
    instance_counts = {int(row["tenant_id"]): int(row["cnt"] or 0) for row in instance_rows}
    member_counts = {int(row["tenant_id"]): int(row["cnt"] or 0) for row in member_rows}
    return instance_counts, member_counts


def build_tenant_info(tenant: DBTenant, instance_count: int, member_count: int) -> TenantInfo:
    """把租户行组装为对外契约对象

    `status` 在库里是自由字符串，无法识别时按归档处理：一个状态坏掉的租户宁可对外
    表现为"已归档"，也不要被当成 active 继续放行。
    """
    try:
        status = TenantStatus(tenant.status)
    except ValueError:
        logger.warning(f"租户 {tenant.name} 的 status 取值非法: {tenant.status!r}，按已归档展示")
        status = TenantStatus.ARCHIVED

    return TenantInfo(
        id=tenant.id,
        name=tenant.name,
        display_name=tenant.display_name,
        status=status,
        instance_count=instance_count,
        member_count=member_count,
        contact=tenant.contact,
        note=tenant.note,
        create_time=tenant.create_time.strftime(DATETIME_FORMAT) if tenant.create_time else None,
    )


async def describe_tenant(tenant: DBTenant) -> TenantInfo:
    """为单个租户组装详情"""
    instance_count = await DBAdapterInstance.filter(tenant_id=tenant.id).count()
    member_count = await DBUser.filter(tenant_id=tenant.id).count()
    return build_tenant_info(tenant, instance_count, member_count)


def build_member_info(user: DBUser) -> TenantMemberInfo:
    return TenantMemberInfo(
        user_id=user.id,
        username=user.username,
        role=tenant_role_of(user),
        is_active=user.is_active,
    )


@router.get("/tenants", summary="获取租户列表")
async def list_tenants(_current_user: DBUser = Depends(require_platform_owner())) -> TenantListResponse:
    """列出全部租户（含已归档），仅平台方可用"""
    tenants = await DBTenant.all().order_by("id")
    instance_counts, member_counts = await count_by_tenant()
    items = [
        build_tenant_info(tenant, instance_counts.get(tenant.id, 0), member_counts.get(tenant.id, 0)) for tenant in tenants
    ]
    return TenantListResponse(items=items, total=len(items))


@router.post("/tenants", summary="新建租户")
async def create_tenant(
    body: TenantCreateRequest,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> TenantInfo:
    """新建租户，仅平台方可用。`name` 创建后不可修改"""
    name = body.name.strip().lower()
    if not TENANT_NAME_PATTERN.match(name):
        raise ValidationError(reason="租户标识仅允许小写字母、数字与下划线，长度 2-64")
    if await DBTenant.filter(name=name).exists():
        raise ConflictError(resource=f"租户 '{name}'")

    tenant = await DBTenant.create(
        name=name,
        display_name=body.display_name.strip() or name,
        status=TenantStatus.ACTIVE.value,
        contact=body.contact.strip(),
        note=body.note.strip(),
    )
    logger.info(f"创建租户: id={tenant.id}, name={name}")
    return build_tenant_info(tenant, 0, 0)


@router.get("/tenants/{tenant_id}", summary="获取租户详情")
async def get_tenant(
    tenant_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TenantInfo:
    """获取租户详情；租户只能看自己，看别人一律 404

    **`note` 是平台方写给自己看的客户备注，对租户一律清空。** 它与 `contact` 不是一回事：
    `contact` 是这个客户自己的联系方式（他本来就知道），`note` 是平台方在「客户资料」卡片里
    记的内部备注——报价、催款、对接情况都可能落在这里。

    这条以前没有实际泄漏面，因为唯一会读它的租户侧页面并不存在；`/console/team`
    （租户自助的「我的团队」）上线之后，每个租户成员打开那一页都会把这段文字取回浏览器
    ——页面不渲染它并不算数，响应体里有就是发出去了（与 #29「私钥不该出现在任何一次
    HTTP 响应里」同一条判据）。

    平台方一侧逐字不变：它要在客户资料卡片里读写这段备注。
    """
    tenant = await assert_tenant_accessible(_current_user, tenant_id)
    info = await describe_tenant(tenant)
    if not is_platform_owner(_current_user):
        info.note = ""
    return info


@router.patch("/tenants/{tenant_id}", summary="更新租户")
async def update_tenant(
    tenant_id: int,
    body: TenantUpdateRequest,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> TenantInfo:
    """更新租户的可变属性，仅平台方可用

    `status` 改成 `suspended` 会立即生效：该租户成员无法再登录，其名下实例停止收发消息
    （闸门见 `DBAdapterInstance.is_instance_active`）。已签发的令牌不会被吊销，
    但每次请求都会重新回查数据库，因此登录态最多残留到下一次鉴权。
    """
    tenant = await DBTenant.get_or_none(id=tenant_id)
    if tenant is None:
        raise NotFoundError(resource=f"租户 '{tenant_id}'")

    if body.display_name is not None:
        tenant.display_name = body.display_name.strip()
    if body.contact is not None:
        tenant.contact = body.contact.strip()
    if body.note is not None:
        tenant.note = body.note.strip()
    if body.status is not None and body.status.value != tenant.status:
        logger.warning(f"租户状态变更: name={tenant.name}, {tenant.status} -> {body.status.value}")
        tenant.status = body.status.value

    await tenant.save()
    return await describe_tenant(tenant)


@router.delete("/tenants/{tenant_id}", summary="归档租户")
async def archive_tenant(
    tenant_id: int,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ActionOkResponse:
    """归档租户，仅平台方可用

    只置状态、**不删任何数据**：成员账号、实例绑定、会话与消息全部保留可查。
    归档后该租户等同停用（成员无法登录、实例停止收发）。
    """
    tenant = await DBTenant.get_or_none(id=tenant_id)
    if tenant is None:
        raise NotFoundError(resource=f"租户 '{tenant_id}'")

    tenant.status = TenantStatus.ARCHIVED.value
    await tenant.save(update_fields=["status", "update_time"])
    logger.warning(f"归档租户: id={tenant.id}, name={tenant.name}")
    return ActionOkResponse(ok=True, message="租户已归档，数据保留可查")


@router.get("/tenants/{tenant_id}/members", summary="获取租户成员列表")
async def list_tenant_members(
    tenant_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[TenantMemberInfo]:
    """列出租户成员；租户只能看自己的成员，看别人一律 404"""
    tenant = await assert_tenant_accessible(_current_user, tenant_id)
    members = await DBUser.filter(tenant_id=tenant.id).order_by("id")
    return [build_member_info(member) for member in members]


@router.post("/tenants/{tenant_id}/members", summary="创建租户成员")
async def create_tenant_member(
    tenant_id: int,
    body: TenantMemberCreateRequest,
    _current_user: DBUser = Depends(require_tenant_admin()),
) -> TenantMemberInfo:
    """创建一个租户成员账号

    成员账号是纯后台账号（`adapter_key` 为空串），与聊天平台用户共用 `user` 表但互不干扰。

    两条安全约束：

    1. 不允许创建 `platform_owner`——那等于让租户自助提权成平台方。
    2. `perm_level` 一律由 `tenant_perm_level()` 推导，租户成员的上限被钉死在 `Role.Admin`。
       Console 的实例与容器接口要求 `Role.Super`，租户拿到就等于拿到宿主机 root。
    """
    tenant = await assert_tenant_accessible(_current_user, tenant_id)

    if body.role is TenantRole.PLATFORM_OWNER:
        raise ValidationError(reason="不能在租户下创建平台方账号")

    username = body.username.strip()
    if not username:
        raise ValidationError(reason="登录名不能为空")
    if username == RESERVED_USERNAME:
        raise ValidationError(reason=f"登录名 '{RESERVED_USERNAME}' 为平台方保留")
    if not body.password:
        raise ValidationError(reason="初始密码不能为空")
    # 后台账号之间必须唯一：登录与令牌解析都按 (username, adapter_key="") 定位
    if await DBUser.filter(username=username, adapter_key="").exists():
        raise ConflictError(resource=f"登录名 '{username}'")

    member = await DBUser.create(
        username=username,
        password=get_hashed_password(body.password),
        adapter_key="",
        platform_userid="",
        perm_level=tenant_perm_level(body.role),
        login_time=db_now(),
        tenant_id=tenant.id,
        tenant_role=body.role.value,
    )
    logger.info(f"创建租户成员: tenant={tenant.name}, username={username}, role={body.role.value}")
    return build_member_info(member)


@router.delete("/tenants/{tenant_id}/members/{user_id}", summary="移除租户成员")
async def remove_tenant_member(
    tenant_id: int,
    user_id: int,
    _current_user: DBUser = Depends(require_tenant_admin()),
) -> ActionOkResponse:
    """移除租户成员

    这里是**删除账号行**而不是解绑：`tenant_id` 置空的账号会被判定成平台方，
    解绑等于把一个客户账号提权成全局可见，是明确不能接受的结果。
    成员账号本身只为该租户而存在，随租户关系一并消失是正确语义。
    """
    tenant = await assert_tenant_accessible(_current_user, tenant_id)

    member = await DBUser.get_or_none(id=user_id, tenant_id=tenant.id)
    if member is None:
        # 用户不存在、或属于别的租户，对外不可区分
        raise NotFoundError(resource=f"租户成员 '{user_id}'")
    if member.id == _current_user.id:
        raise ValidationError(reason="不能移除自己")

    await member.delete()
    logger.warning(f"移除租户成员: tenant={tenant.name}, username={member.username}")
    return ActionOkResponse(ok=True, message="成员已移除")


@router.put("/tenants/{tenant_id}/instances", summary="绑定租户实例")
async def bind_tenant_instances(
    tenant_id: int,
    body: TenantInstanceBindRequest,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> TenantInfo:
    """把一组实例划归该租户，仅平台方可用

    请求体是该租户的**完整实例集合**（幂等覆盖）：不在列表里的既有绑定会被解除，
    显式传空列表即解绑全部。

    正因为是覆盖语义，「没带 `instance_keys` 字段」必须与「显式传 `[]`」区分开：前者是
    客户端出了问题，按空列表处理会把一次失败的请求变成一次全量解绑。因此缺字段一律 400。

    已归属其他租户的实例会被拒绝而不是静默改判——那等于把一个客户的 Bot 转交给另一个客户，
    必须由平台方先显式解绑，把这一步变成两次有意识的操作。
    """
    tenant = await DBTenant.get_or_none(id=tenant_id)
    if tenant is None:
        raise NotFoundError(resource=f"租户 '{tenant_id}'")

    if body.instance_keys is None:
        raise ValidationError(reason="必须显式提供 instance_keys；解绑全部请传空列表")

    wanted = [key.strip() for key in body.instance_keys if key.strip()]
    if len(set(wanted)) != len(wanted):
        raise ValidationError(reason="实例标识存在重复")

    # 按标识取回的是**全部同名行**：`instance_key` 只在同一适配器内唯一，下面的整批更新也会
    # 落到每一行上。冲突判定因此必须逐行看——按标识折叠成一行的话，留下来的恰好是未归属的
    # 那一行时判定放行，更新却把另一个适配器下已归属别的租户的同名实例一并改判过来。
    instances = await DBAdapterInstance.filter(instance_key__in=wanted) if wanted else []
    found_keys = {instance.instance_key for instance in instances}
    missing = [key for key in wanted if key not in found_keys]
    if missing:
        raise NotFoundError(resource=f"实例 {missing}")

    conflicted = sorted(
        {
            instance.instance_key
            for instance in instances
            if instance.tenant_id is not None and instance.tenant_id != tenant.id
        },
    )
    if conflicted:
        raise ValidationError(reason=f"实例 {conflicted} 已归属其他租户，请先解绑")

    stale = DBAdapterInstance.filter(tenant_id=tenant.id)
    if wanted:
        stale = stale.exclude(instance_key__in=wanted)
    # 解绑是覆盖语义里唯一有破坏性的一步，先取出被摘掉的实例再更新，让它在日志里留痕
    stale_rows = await stale
    released = [row.instance_key for row in stale_rows]
    # 划入与收回的实例，令牌此刻都已经（或马上）在租户手里：先记下来再改归属，记不下来就拒绝这次操作、库里什么都不改。
    # 收回之后它就是平台方实例了，不记的话前租户手里那枚令牌又成了冒充超级用户的钥匙，而全量重建补不回它
    record_instances_exposure(
        [*stale_rows, *instances], reason=f"租户 {tenant.id} 的实例归属变更", refuse_on_failure=True,
    )
    await stale.update(tenant_id=None)
    if wanted:
        await DBAdapterInstance.filter(instance_key__in=wanted).update(tenant_id=tenant.id)

    # 归属真的变了的那些实例必须退出它此前所在的记忆/好感域，否则就是跨租户串域：
    # 域的归属校验只在"加入实例组的那一刻"成立（`assert_instance_joinable`），
    # 之后读侧不复查（每条消息多查两张表的代价见 `services/identity_scope.py` 的缓存取舍）。
    # 不清的话，被划走的实例会继续读原租户的记忆与好感，被划入的实例会把自己的带进新租户。
    # **只清归属真的变了的**：本接口是幂等覆盖语义，原样重放一次不该把域配置一并抹掉。
    reassigned = sorted({instance.instance_key for instance in instances if instance.tenant_id != tenant.id})
    dropped = await DBInstanceDomainBinding.release_instances(
        [*released, *reassigned],
        reason=f"租户实例归属变更: tenant={tenant.name}",
    )

    logger.info(f"更新租户实例绑定: tenant={tenant.name}, instances={wanted}, 域绑定释放={dropped}")
    if released:
        logger.warning(f"租户实例解绑: tenant={tenant.name}, released={released}")
    return await describe_tenant(tenant)
