"""总后台租户管理路由

## 权限分层

- **平台方专属**：租户的增删改查、以及"把实例划归某租户"。实例归属是商业关系的落点，
  租户管理员绝不能自助把别人的实例划到自己名下，因此 `PUT /instances` 是平台方专属。
- **平台方或租户管理员**：成员的增删。租户管理员只能给自己团队建**只读成员**；
  新增管理员由平台方来建（把「提权」这一步留在平台方手里）。
- **任意已登录身份（作用域受限）**：读取租户详情与成员列表。

所有带 `{tenant_id}` 的端点都先过 `assert_tenant_accessible`，租户访问不属于自己的
租户一律得到 **404** 而非 403 —— 403 会把"这个租户 ID 存在"泄露出去，
让平台方的客户清单变成可枚举的。

## 为什么业务逻辑写在路由层

租户的读写逻辑本应落在 `services/console/`，但该目录由并行开发的其他模块占用。
本模块暂时自包含，待并行开发合流后可整体下沉，届时路由只保留参数校验与依赖声明。
"""

import re
from dataclasses import dataclass
from functools import reduce
from typing import ClassVar, Dict, List, Tuple

from fastapi import APIRouter, Depends
from tortoise.expressions import Q
from tortoise.functions import Count
from tortoise.timezone import now as db_now

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaPolicy
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ActionOkResponse
from nekro_agent.schemas.errors import AppError, ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.schemas.tenant import (
    MIN_INITIAL_PASSWORD_LENGTH,
    TenantCreateRequest,
    TenantInfo,
    TenantInstanceBindRequest,
    TenantInstanceTransferChange,
    TenantInstanceTransferPreview,
    TenantListResponse,
    TenantMemberCreateRequest,
    TenantMemberInfo,
    TenantRole,
    TenantStatus,
    TenantUpdateRequest,
)
from nekro_agent.services.console.tenant_manager import set_tenant_status
from nekro_agent.services.emotion_library import (
    clear_collect_set,
    count_instance_emotions,
    revoke_instance_assignments,
    revoke_whiteboard_view_token,
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


class TenantAdminCanOnlyCreateReadOnlyMemberError(AppError):
    """租户管理员只能创建只读成员

    租户管理员本身是 `Role.Admin`，能自助建成员。如果它还能建一个同样是
    `Role.Admin` 的租户管理员，那么「谁有管理权」这件事就完全落在租户自己手里：
    一个客户的主账号被他人接管之后，接管者可以再建一个管理员账号当作后门，
    平台方停用原账号也止不住。所以把「提权」这一步留在平台方手里——新增管理员
    只能由平台方在租户详情里建，租户管理员建的账号一律只读。

    403 而不是 404：这里不掩盖任何存在性（租户本身已经过 `assert_tenant_accessible`），
    这是一条「换个操作者来做」的能力边界。
    """

    http_status: ClassVar[int] = 403
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="租户管理员只能创建只读成员；新增管理员请联系平台方",
        en_US="Tenant admins can only create read-only members; ask the platform operator to add another admin",
    )


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

    if body.status is not None:
        await set_tenant_status(tenant, body.status.value)
    else:
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

    三条安全约束：

    1. 不允许创建 `platform_owner`——那等于让租户自助提权成平台方。
    2. `perm_level` 一律由 `tenant_perm_level()` 推导，租户成员的上限被钉死在 `Role.Admin`。
       Console 的实例与容器接口要求 `Role.Super`，租户拿到就等于拿到宿主机 root。
    3. **租户管理员只能建只读成员**，新增管理员由平台方来建。理由见
       `TenantAdminCanOnlyCreateReadOnlyMemberError`：把「提权」这一步留在平台方手里。
       注意 `TenantMemberCreateRequest.role` 的默认值仍是 `TENANT_ADMIN`，租户管理员
       不带 `role` 时会直接被这条拒掉，这是预期的。

    创建者记在 `ext_data.created_by`（`user_id` / `username` / `role`）。`DBUser` 没有专门的
    「谁建的」列，而 `ext_data` 本来就是 JSONField；因此不需要加迁移。
    """
    tenant = await assert_tenant_accessible(_current_user, tenant_id)

    if body.role is TenantRole.PLATFORM_OWNER:
        raise ValidationError(reason="不能在租户下创建平台方账号")

    if not is_platform_owner(_current_user) and body.role is not TenantRole.TENANT_USER:
        raise TenantAdminCanOnlyCreateReadOnlyMemberError

    username = body.username.strip()
    if not username:
        raise ValidationError(reason="登录名不能为空")
    if username == RESERVED_USERNAME:
        raise ValidationError(reason=f"登录名 '{RESERVED_USERNAME}' 为平台方保留")
    if len(body.password) < MIN_INITIAL_PASSWORD_LENGTH:
        raise ValidationError(reason=f"初始密码至少 {MIN_INITIAL_PASSWORD_LENGTH} 位")
    # 后台账号之间必须唯一：登录与令牌解析都按 (username, adapter_key="") 定位
    if await DBUser.filter(username=username, adapter_key="").exists():
        raise ConflictError(resource=f"登录名 '{username}'")

    creator_role = tenant_role_of(_current_user)
    member = await DBUser.create(
        username=username,
        password=get_hashed_password(body.password),
        adapter_key="",
        platform_userid="",
        perm_level=tenant_perm_level(body.role),
        login_time=db_now(),
        tenant_id=tenant.id,
        tenant_role=body.role.value,
        ext_data={
            "created_by": {
                "user_id": _current_user.id,
                "username": _current_user.username,
                "role": creator_role.value,
            },
        },
    )
    logger.info(
        f"创建租户成员: tenant={tenant.name}, username={username}, role={body.role.value}, "
        f"created_by={_current_user.username}, created_by_role={creator_role.value}",
    )
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


@dataclass(frozen=True)
class InstanceBindingPlan:
    """一次「整组替换实例」的判定结果（预览与保存共用）

    `released` / `reassigned` 都是 `instance_key`，因为 `release_instances` 的既有语义就是
    按这个 slug 批量释放域绑定；`changed_rows` 则回到**行**：同名实例在不同适配器下是
    两份归属、两份数据，清空记忆时必须逐行处理。
    """

    tenant: DBTenant
    wanted: List[str]
    instances: List[DBAdapterInstance]
    stale_rows: List[DBAdapterInstance]
    released: List[str]
    reassigned: List[str]

    @property
    def changed_rows(self) -> List[Tuple[DBAdapterInstance, bool]]:
        """改变归属的实例行与「是否被收回」；收回的是本租户原有行，划入的是新行。"""
        released_ids = {row.id for row in self.stale_rows}
        rows = [*self.stale_rows, *[row for row in self.instances if row.tenant_id != self.tenant.id]]
        return [(row, row.id in released_ids) for row in rows]


async def _plan_instance_binding(tenant_id: int, body: TenantInstanceBindRequest) -> InstanceBindingPlan:
    """校验并算出这次保存会改变哪些实例的归属，不写任何东西。

    预览端点与保存端点必须共用这一段，否则「预览看到的条数」和「保存时实际清掉的实例」
    会在某个新增校验维度上分叉——预览少算一个实例，保存就会删掉对话框里没提示的那份数据。
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
    # 归属真的变了的那些实例：本接口是幂等覆盖语义，原样重放一次不该把域配置一并抹掉。
    reassigned = sorted({instance.instance_key for instance in instances if instance.tenant_id != tenant.id})
    return InstanceBindingPlan(
        tenant=tenant,
        wanted=wanted,
        instances=list(instances),
        stale_rows=list(stale_rows),
        released=released,
        reassigned=reassigned,
    )


async def _clear_tenant_quota_settings(rows: List[Tuple[DBAdapterInstance, bool]]) -> None:
    """清掉这批实例上旧租户设的频道额度与暂停提示

    为什么**不受** `clear_memory_and_affection` 控制：那是旧主人对他自己的群设的额度与
    提示语，实例换了主人就不该被新主人继承，也没有任何「留着」的理由——留着只会让新租户
    看到一个自己从没填过的数字，而它还是按旧租户的单位快照算的。清空是归属变更的一部分，
    与「要不要顺手删记忆」是两个互不相干的问题。

    范围是 `(adapter_key, instance_key)` 下的**所有**策略行：实例层那一行与它名下每个频道
    层那一行。只清实例层会留下频道层里更细的旧设置，而解析时频道层优先级更高，等于没清。

    平台方字段（开关、单位、额度、超额动作、提示语）一律不动：那是平台方给这个实例的承诺，
    与实例归谁无关。清完必须让策略缓存失效，否则判定路径还会按旧值拦最多一分钟。
    """
    from nekro_agent.services.channel_quota import policy as policy_module

    conditions = [
        Q(adapter_key=row.adapter_key, instance_key=row.instance_key) for row, _ in rows
    ]
    if not conditions:
        return
    await DBChannelQuotaPolicy.filter(reduce(lambda left, right: left | right, conditions)).update(
        tenant_group_daily_limit=None,
        tenant_private_daily_limit=None,
        tenant_limit_unit=None,
        tenant_pause_message=None,
    )
    policy_module.invalidate_policy_cache()


async def _count_tenant_quota_settings(adapter_key: str, instance_key: str) -> int:
    """这个实例上有多少行带租户额度设置（四个租户字段任一非空）

    预览里报的是**行数**而不是字段数：一行就是一处「旧租户在这个实例上留过东西」，
    四个字段各填一个与只填一个是同一处设置。
    """
    return await DBChannelQuotaPolicy.filter(
        Q(adapter_key=adapter_key, instance_key=instance_key)
        & (
            Q(tenant_group_daily_limit__not_isnull=True)
            | Q(tenant_private_daily_limit__not_isnull=True)
            | Q(tenant_limit_unit__not_isnull=True)
            | Q(tenant_pause_message__not_isnull=True)
        )
    ).count()


async def _count_instance_ssh_credentials(adapter_key: str, instance_key: str) -> int:
    """这个实例对上用户登记的服务器凭据行数（平台方行没有实例对，不在此列）"""
    from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential

    return await DBWsSshCredential.filter(
        adapter_key=adapter_key,
        instance_key=instance_key,
        owner_kind="user",
    ).count()


async def _count_instance_mailboxes(adapter_key: str, instance_key: str) -> Tuple[int, int]:
    """这个实例对上自己填的邮箱账户数与用户邮件偏好数"""
    from nekro_agent.models.db_email_account import DBEmailAccount
    from nekro_agent.models.db_email_user_pref import DBEmailUserPref

    return (
        await DBEmailAccount.filter(owner_adapter_key=adapter_key, owner_instance_key=instance_key).count(),
        await DBEmailUserPref.filter(adapter_key=adapter_key, instance_key=instance_key).count(),
    )


async def _delete_instance_mailboxes(
    rows: List[Tuple[DBAdapterInstance, bool]],
    *,
    tenant_name: str,
) -> None:
    """删掉这些实例对上自己填的邮箱账户（`delete_instance_mailboxes` 勾着时）

    只删改变归属的实例；调用方传进来的就是 `plan.changed_rows`，原样重放时为空。

    删账户**连缓存邮件与附件目录一起删**：账户没了，那些信既没有主人在控制台看得见，也还占着
    `(地址, UID)` 唯一键，同名地址将来被别人填进来就会看到上一个人的邮件。附件按
    `email_accounts.plan_attachment_cleanup` 删：`_i<数字>` 形态（实例自有账户新建时的命名）整个
    目录删，由平台方转来的地址目录只删这些账户被删掉的那几封——整个删错一个平台方账户的目录
    是没法恢复的。附件与运行时客户端是两件事：适配器没加载时客户端本来就不存在，附件照删。

    平台方邮箱的点名授权与未发的节日邮件不在这里处理：不管勾不勾都要处理，见
    `_release_instance_mail_grants`。不做进事务里（文件系统与数据库不是同一条回滚路径），
    逐实例失败只记警告。
    """
    from nekro_agent.adapters import loaded_adapters
    from nekro_agent.models.db_email import DBEmail
    from nekro_agent.models.db_email_account import DBEmailAccount
    from nekro_agent.services import email_accounts as email_account_service

    adapter = loaded_adapters.get("email")

    for instance_row, _is_released in rows:
        account_rows = await DBEmailAccount.filter(
            owner_adapter_key=instance_row.adapter_key,
            owner_instance_key=instance_row.instance_key,
        ).all()
        if not account_rows:
            continue
        # 先断开客户端，再算附件路径：客户端还连着时会接着收信、下附件，断开之前新到的那封也要在清单里
        for account_row in account_rows:
            if adapter is not None:
                runtime = email_account_service.to_runtime_account(account_row)
                try:
                    await email_account_service.remove_runtime_account(adapter, runtime)
                except Exception as e:  # noqa: BLE001 - 客户端关不掉不该让划转失败
                    logger.warning(f"划转时断开邮箱客户端失败: {type(e).__name__}")
        # 附件路径要在删缓存行之前算好（地址目录按行逐封删，删完行就查不到 UID 了）
        attachment_cleanup = await email_account_service.plan_attachment_cleanup(account_rows)
        await DBEmail.filter(
            owner_adapter_key=instance_row.adapter_key,
            owner_instance_key=instance_row.instance_key,
        ).delete()
        await DBEmailAccount.filter(
            owner_adapter_key=instance_row.adapter_key,
            owner_instance_key=instance_row.instance_key,
        ).delete()
        email_account_service.run_attachment_cleanup(attachment_cleanup)
        email_account_service.clear_default_sender_cache()
        logger.warning(
            f"租户实例划转时删除实例自有邮箱账户: tenant={tenant_name}, "
            f"adapter_key={instance_row.adapter_key}, instance_key={instance_row.instance_key}, "
            f"deleted={len(account_rows)}",
        )


async def _release_instance_mail_grants(
    rows: List[Tuple[DBAdapterInstance, bool]],
    *,
    tenant_name: str,
) -> None:
    """划转时不管勾不勾「删自有邮箱」都要做的两件事

    1. 平台方邮箱 `SEND_SHARE_INSTANCES` 里点名这个实例的项去掉：那条授权是平台方给前一个主人的，
       实例换了主人还留着，等于把平台方的发信能力顺手送给新租户（`all_instances` 的照旧，那是
       平台方对所有实例的显式选择）；
    2. 这个实例还没发出去的节日邮件作废（`skipped`、清正文）：收件人名单与发件邮箱都是按前一个
       主人算的，不能在新主人名下接着发。
    """
    from tortoise.timezone import now as db_now

    from nekro_agent.models.db_email_outbox import DBEmailOutbox
    from nekro_agent.services.email_accounts import rewrite_platform_share_instances

    for instance_row, _is_released in rows:
        revoked = rewrite_platform_share_instances(f"{instance_row.adapter_key}:{instance_row.instance_key}", None)
        # 查询集的 `update()` 不触发 `auto_now`：`update_time` 要手写，否则 7 天清理按入队时刻算
        voided = await DBEmailOutbox.filter(
            adapter_key=instance_row.adapter_key,
            instance_key=instance_row.instance_key,
            status__in=["queued", "paused"],
        ).update(status="skipped", body="", next_at=None, update_time=db_now())
        if revoked or voided:
            logger.info(
                f"租户实例划转时收回邮箱授权: tenant={tenant_name}, adapter_key={instance_row.adapter_key}, "
                f"instance_key={instance_row.instance_key}, 取消共享={revoked}, 作废节日邮件={voided}",
            )


async def _delete_instance_settings(
    rows: List[Tuple[DBAdapterInstance, bool]],
    *,
    plugin_settings: bool,
    config_overrides: bool,
    tenant_name: str,
) -> None:
    """删掉这些实例名下的设置（`delete_plugin_settings` / `delete_config_overrides` 勾着的那几样）

    插件设置：实例级启停、这个实例的频道上的启停、`plugin_data/<插件>/instances/<实例>/`——上一任
    主人给插件填的东西（API Key、Webhook、私有地址）。配置覆盖：核心配置的实例层与这个实例频道上的
    频道层覆盖——告警 Webhook、指向前租户自己模型组的选择、提示词与过滤词表。跟着实例划给新租户
    等于直接交出去。只删改变归属的实例；调用方传进来的就是 `plan.changed_rows`，原样重放时为空。
    出错直接抛出，位置在改归属之前：删不掉就整次划转失败、归属不改，重试会接着删。
    """
    from nekro_agent.services import instance_settings

    for instance_row, _is_released in rows:
        chat_keys = await instance_settings.instance_chat_keys(instance_row.adapter_key, instance_row.instance_key)
        counts = await instance_settings.drop_instance_settings(
            instance_row.adapter_key,
            instance_row.instance_key,
            chat_keys,
            plugin_settings=plugin_settings,
            config_overrides=config_overrides,
        )
        if any(counts.values()):
            logger.warning(
                f"租户实例划转时删除插件设置 / 配置覆盖: tenant={tenant_name}, adapter_key={instance_row.adapter_key}, "
                f"instance_key={instance_row.instance_key}, counts={counts}",
            )


async def _redrop_instance_settings(
    rows: List[Tuple[DBAdapterInstance, bool]],
    *,
    plugin_settings: bool,
    config_overrides: bool,
    tenant_name: str,
) -> None:
    """改归属之后对这些实例再删一遍设置，失败只记警告（归属已经改了，不能再让划转报错）

    改归属之前那一遍（`_delete_instance_settings`）与改库之间，前一个主人的后台页面还开着、还能写；
    这一遍把那段时间写进去的删掉。删不动的残留在建同名实例前的兜底清扫里还会再扫。
    """
    from nekro_agent.services import instance_settings

    for instance_row, _is_released in rows:
        try:
            chat_keys = await instance_settings.instance_chat_keys(instance_row.adapter_key, instance_row.instance_key)
        except Exception as e:  # noqa: BLE001 - 归属已改，补删失败只记警告
            logger.warning(
                f"租户 {tenant_name} 的实例划转改归属之后补删设置失败: {instance_row.adapter_key}/"
                f"{instance_row.instance_key} | {type(e).__name__}",
            )
            continue
        counts = await instance_settings.drop_instance_settings_quietly(
            instance_row.adapter_key,
            instance_row.instance_key,
            chat_keys,
            plugin_settings=plugin_settings,
            config_overrides=config_overrides,
            reason=f"租户 {tenant_name} 的实例划转改归属之后",
        )
        if any(counts.values()):
            logger.warning(
                f"租户实例划转期间前一个主人又写入了设置，改归属之后已删除: tenant={tenant_name}, "
                f"adapter_key={instance_row.adapter_key}, instance_key={instance_row.instance_key}, counts={counts}",
            )


async def _refuse_shared_plugin_settings(rows: List[Tuple[DBAdapterInstance, bool]]) -> None:
    """同一个实例标识在别的适配器下还有一行、它不跟着改归属，而这个标识上有插件的实例级设置：拒绝

    插件的实例级设置只按实例标识存，两行共用一份——删了连那一行的一起删，留着又等于交给新主人。
    与彻底删除遇到跨适配器同名时同一个做法：报错，让平台方先处理同名（启动日志里有告警）。
    在任何改动之前查，拒绝时什么都没动。
    """
    from nekro_agent.services.instance_settings import shared_instance_level_conflict

    moving_ids = [row.id for row, _ in rows]
    for instance_row, _is_released in rows:
        if await shared_instance_level_conflict(instance_row.adapter_key, instance_row.instance_key, moving_ids):
            raise ValidationError(
                reason=(
                    f"实例标识 '{instance_row.instance_key}' 在别的适配器下还有一个实例、这次不改归属，"
                    "而插件的实例级设置按实例标识存、两者共用一份；请先处理同名实例，或取消勾选「一并删除插件设置」"
                ),
            )


async def _clear_instance_email_user_prefs(rows: List[Tuple[DBAdapterInstance, bool]]) -> None:
    """清空这些实例上的用户邮件偏好（勾了「清空记忆」时）

    偏好里是**用户**的邮箱地址与订阅意愿：划到新租户之后还留着，等于把上一任租户的用户
    名单交给新主人。这里**整行删掉，退订标记也不留**：「一并清空」是平台方对这次转手的
    显式选择——新主人名下重新开始，与记忆、好感一起清。平时后台删一行偏好仍保留退订标记
    （`instance_email.delete_user_pref`），那是同一个主人名下的操作，不能借它把人订阅回来。
    """
    from nekro_agent.models.db_email_user_pref import DBEmailUserPref

    for instance_row, _is_released in rows:
        updated = await DBEmailUserPref.filter(
            adapter_key=instance_row.adapter_key,
            instance_key=instance_row.instance_key,
        ).delete()
        if updated:
            logger.warning(
                f"租户实例划转时清空用户邮件偏好: adapter_key={instance_row.adapter_key}, "
                f"instance_key={instance_row.instance_key}, rows={updated}",
            )


async def _delete_instance_ssh_credentials(
    rows: List[Tuple[DBAdapterInstance, bool]],
    *,
    actor: str,
    tenant_name: str,
) -> None:
    """删掉这些实例对上用户登记的服务器凭据，逐行留 `cred_delete` 审计

    凭据是「能进别人家门」的东西，误跟过去比误删代价大：划转默认删除（与邮箱划转同一条
    取舍）。只删改变归属的实例；调用方传进来的就是 `plan.changed_rows`，原样重放时为空。
    审计行不随凭据删除消失，`actor` 记操作的后台用户名，`detail` 只写事由、不写任何值。
    """
    from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
    from nekro_agent.services.ssh_gateway import constants as ssh_constants
    from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service

    for instance_row, _is_released in rows:
        credential_rows = await DBWsSshCredential.filter(
            adapter_key=instance_row.adapter_key,
            instance_key=instance_row.instance_key,
            owner_kind=ssh_constants.OWNER_USER,
        ).all()
        if not credential_rows:
            continue
        # 先写审计再删行：快照来自行本身，删完就没有可读的归属了
        for credential_row in credential_rows:
            await ssh_credential_service._write_audit(  # noqa: SLF001 - 与凭据服务共用同一份审计写法
                credential_row,
                event=ssh_constants.EVENT_CRED_DELETE,
                allowed=True,
                actor=actor,
                detail="实例划转",
            )
        await DBWsSshCredential.filter(
            adapter_key=instance_row.adapter_key,
            instance_key=instance_row.instance_key,
            owner_kind=ssh_constants.OWNER_USER,
        ).delete()
        # 通知网关：用着这些行的连接立刻断开、上游关掉、容器里的别名下一次刷新时去掉
        from nekro_agent.services.ssh_gateway import events as ssh_events

        for credential_row in credential_rows:
            await ssh_events.notify_credential_changed(int(credential_row.id))
        logger.warning(
            f"租户实例划转时删除用户登记的服务器凭据: tenant={tenant_name}, "
            f"adapter_key={instance_row.adapter_key}, instance_key={instance_row.instance_key}, "
            f"deleted={len(credential_rows)}",
        )


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

    `clear_memory_and_affection` 为真时，真删**改变归属**的实例的域行记忆、摘要、提示词明细、
    世界书与自成一域的好感。顺序是「先记令牌曝光、再记工作区标记、再清空、最后改归属」：清空
    失败时请求报错、归属一个都没改，原样重试时这些实例仍是「改变归属的行」，会接着清完
    再改归属。反过来先改归属再清的话，清到一半失败后再点一次保存，这些实例已经归了新
    租户、不再算「改变归属」，清空整段被跳过且不报错，旧租户的记忆就留给了新租户。
    清空的目标只由实例对决定（域行按写入它的实例记，好感只删自成一域那一份），与此刻
    是否还在实例组里无关，所以放在释放域绑定之前不改变删什么。

    **旧租户的频道额度设置每次都清**，不受 `clear_memory_and_affection` 控制：那是旧主人
    对他自己的群设的额度与提示语，新主人不该继承（理由见 `_clear_tenant_quota_settings`）。
    位置与清记忆同一条：在改归属之前，失败就报错、归属不改，重试时这些实例仍算「改变归属」，
    会接着清完再改。原样重放（集合没变）时 `changed_rows` 为空，什么都不清。

    **用户登记的服务器凭据默认删除**（`delete_ssh_credentials`）：与邮箱划转同一条取舍，
    凭据是「能进别人家门」的东西，误跟过去比误删代价大。同样在改归属之前处理，失败就报错、
    归属不改；取消勾选时才让行跟着实例走。平台方行没有实例归属，不受划转影响。

    **实例自己填的邮箱默认删除**（`delete_instance_mailboxes`）：那也是上一任主人的收件箱
    （密码重置、验证码、账单），跟着实例划给新租户等于直接交出去。删账户连带删这些账户的
    缓存邮件与附件目录；只作用于改变归属的实例，位置同上。取消勾选时账户行跟着实例走，
    新租户在后台看得见也能改——那是一次有意识的整体转交。不管勾不勾，平台方邮箱点名这个实例的
    发信授权都取消、未发的节日邮件都作废（那是给前一个主人的）。
    用户邮件偏好跟着 `clear_memory_and_affection` 走：勾了清空就整行删掉，退订标记也不留
    （新主人名下重新开始）；不勾就跟着实例走。

    **插件按实例 / 按频道的设置默认删除**（`delete_plugin_settings`）：实例级启停、这个实例的
    频道上的启停、实例级插件配置目录。**核心配置的实例层 / 频道层覆盖默认删除**（`delete_config_overrides`）。
    取舍与 SSH 凭据、自有邮箱相同，位置同上；改归属之后再删一遍（失败只记警告），收掉两者之间
    前一个主人写回的。同一个实例标识在别的适配器下还有一行而它不改归属时，
    插件的实例级设置是两行共用的，勾着删插件设置就整次拒绝（在任何改动之前查）。
    """
    plan = await _plan_instance_binding(tenant_id, body)
    if body.delete_plugin_settings:
        await _refuse_shared_plugin_settings(plan.changed_rows)
    # 协议端自动登记时没扫成残留设置的实例（带补扫标记）：先补扫，扫不掉就拒绝，归属不改
    from nekro_agent.services.console.instance_manager import resweep_before_handover

    for instance_row, _is_released in plan.changed_rows:
        await resweep_before_handover(instance_row, "划转")
    tenant = plan.tenant
    wanted = plan.wanted
    instances = plan.instances
    stale_rows = plan.stale_rows
    released = plan.released
    # 划入与收回的实例，令牌此刻都已经（或马上）在租户手里：先记下来再改归属，记不下来就拒绝这次操作、库里什么都不改。
    # 收回之后它就是平台方实例了，不记的话前租户手里那枚令牌又成了冒充超级用户的钥匙，而全量重建补不回它
    record_instances_exposure(
        [*stale_rows, *instances], reason=f"租户 {tenant.id} 的实例归属变更", refuse_on_failure=True,
    )
    # 划入 / 收回租户都会让实例名下频道绑着的工作区变成非平台方归属：同样先记后改，记不下来就拒绝。
    # 收回之后工作区重新算成平台方，前租户手里那张网页聊天令牌就又能用了，而全量重建补不回它
    from nekro_agent.services.mcp.web_chat_workspace_taint import taint_workspaces_bound_to_instance_keys

    await taint_workspaces_bound_to_instance_keys(
        [*released, *wanted],
        reason=f"租户 {tenant.id} 的实例归属变更",
        refuse_on_failure=True,
    )

    # 改归属之前清掉旧租户的额度设置（划入与收回都清）：失败就报错、归属不改，重试会接着清。
    await _clear_tenant_quota_settings(plan.changed_rows)

    # 清空必须在改归属之前（理由见上面的文档）：失败就让请求报错，归属保持原样，重试会接着清。
    if body.clear_memory_and_affection:
        from nekro_agent.services.memory.admin_service import purge_instance_domain_memory

        for row, is_released in plan.changed_rows:
            try:
                purged = await purge_instance_domain_memory(row.adapter_key, row.instance_key)
            except Exception as e:
                logger.error(
                    f"租户 {tenant.name} 的实例划转前清空记忆、世界书与好感失败，归属未改: "
                    f"adapter_key={row.adapter_key}, instance_key={row.instance_key}, error={e}",
                )
                raise
            logger.warning(
                f"租户实例划转时清空记忆、世界书与好感: tenant={tenant.name}, "
                f"adapter_key={row.adapter_key}, instance_key={row.instance_key}, "
                f"action={'released' if is_released else 'assigned'}, counts={purged}",
            )
            # 收集的表情：只清这个实例收集库里的行 / 向量 / 文件，收集库套组本身留个空壳。
            # 套组跟着实例走，下次会话进来还是同一个 ID，配置里引用的 ID 不会失效。
            try:
                cleared = await clear_collect_set(row.adapter_key, row.instance_key)
            except Exception as e:
                logger.error(
                    f"租户 {tenant.name} 的实例划转前清空收集的表情失败，归属未改: "
                    f"adapter_key={row.adapter_key}, instance_key={row.instance_key}, error={e}",
                )
                raise
            logger.warning(
                f"租户实例划转时清空收集的表情: tenant={tenant.name}, "
                f"adapter_key={row.adapter_key}, instance_key={row.instance_key}, "
                f"action={'released' if is_released else 'assigned'}, counts={cleared}",
            )

    # 平台方指向这些实例的套组分配**每次都取消**，不受上面那个开关控制：划转之后实例换了主人，
    # 原先「给这个实例」的授权是给前一个租户的，跟着过去等于把平台方那一份直接送进新租户手里。
    # `all_instances` / `platform_instances` 的分配照旧生效——那本来就是给所有人的。
    for row, _is_released in plan.changed_rows:
        revoked = await revoke_instance_assignments(row.adapter_key, row.instance_key)
        if revoked:
            logger.info(
                f"租户实例划转时取消平台方给它的表情套组分配: tenant={tenant.name}, "
                f"adapter_key={row.adapter_key}, instance_key={row.instance_key}, assignments={revoked}",
            )
        # 白板的只读链接同理是发给前一个主人的，每次都作废（已经连着的浏览器由白板插件复核授权时断开，
        # 板上的内容在新主人第一次用时换成空板，见 `whiteboard._board_state_for_tenant`）
        if await revoke_whiteboard_view_token(row.adapter_key, row.instance_key):
            logger.info(
                f"租户实例划转时作废白板只读链接: tenant={tenant.name}, "
                f"adapter_key={row.adapter_key}, instance_key={row.instance_key}",
            )

    # 用户登记的服务器凭据默认删除，位置与清空记忆同一条：改归属之前处理，失败就报错、
    # 归属保持原样；取消勾选时什么都不做，行跟着实例走（租户按实例现算，不用改行）
    if body.delete_ssh_credentials:
        await _delete_instance_ssh_credentials(
            plan.changed_rows,
            actor=_current_user.username,
            tenant_name=tenant.name,
        )

    # 实例自己填的邮箱默认删除，与 SSH 凭据同一条取舍：那是上一任主人的收件箱，
    # 跟着实例划给新租户等于把密码重置邮件与验证码一起交出去。同样在改归属之前处理，
    # 失败就报错、归属保持原样，重试时这些实例仍算「改变归属」，会接着删完再改。
    if body.delete_instance_mailboxes:
        await _delete_instance_mailboxes(plan.changed_rows, tenant_name=tenant.name)
    # 平台方的点名发信授权与未发的节日邮件不管勾不勾都收回：那是给前一个主人的
    await _release_instance_mail_grants(plan.changed_rows, tenant_name=tenant.name)

    # 插件设置与核心配置覆盖默认删除，与 SSH 凭据、自有邮箱同一条取舍：里面是上一任主人填的 API Key、
    # Webhook、对它自己模型组的选择之类，跟着实例走等于交出去。改归属之前处理，失败就报错、归属保持原样；
    # 取消勾选时什么都不做，设置按实例标识存，自然跟着实例走。
    if body.delete_plugin_settings or body.delete_config_overrides:
        await _delete_instance_settings(
            plan.changed_rows,
            plugin_settings=body.delete_plugin_settings,
            config_overrides=body.delete_config_overrides,
            tenant_name=tenant.name,
        )

    # 用户邮件偏好跟着「清空记忆」走：那是这些用户的地址与订阅意愿，划给新租户就不该留着。
    # 放在同一个位置，理由同上（失败则归属不改，重试会接着清）。
    if body.clear_memory_and_affection:
        await _clear_instance_email_user_prefs(plan.changed_rows)

    await DBAdapterInstance.filter(id__in=[row.id for row in stale_rows]).update(tenant_id=None)
    if wanted:
        await DBAdapterInstance.filter(instance_key__in=wanted).update(tenant_id=tenant.id)
    if body.delete_plugin_settings or body.delete_config_overrides:
        # 改归属之后再删一遍：改归属之前那一遍与改库之间，前一个主人还能往这些实例上写
        await _redrop_instance_settings(
            plan.changed_rows,
            plugin_settings=body.delete_plugin_settings,
            config_overrides=body.delete_config_overrides,
            tenant_name=tenant.name,
        )
    if wanted:
        # 改库后再补一遍：上面置位与改库之间新绑到这些实例频道上的工作区，绑定那一侧查到的还是平台方。
        # 库已经改了，这一遍记不下来不再拒绝，记录会置为待重建（判定一律不合格）
        try:
            await taint_workspaces_bound_to_instance_keys(wanted, reason=f"租户 {tenant.id} 的实例归属变更后复核")
        except Exception as e:  # noqa: BLE001 - 库已改，补记失败只记警告
            logger.warning(f"租户 {tenant.id} 的实例归属变更后补记网页聊天 MCP 工作区标记失败: {e}")

    # 归属真的变了的那些实例必须退出它此前所在的记忆/好感域，否则就是跨租户串域：
    # 域的归属校验只在"加入实例组的那一刻"成立（`assert_instance_joinable`），
    # 之后读侧不复查（每条消息多查两张表的代价见 `services/identity_scope.py` 的缓存取舍）。
    # 不清的话，被划走的实例会继续读原租户的记忆与好感，被划入的实例会把自己的带进新租户。
    # **只清归属真的变了的**：本接口是幂等覆盖语义，原样重放一次不该把域配置一并抹掉。
    dropped = await DBInstanceDomainBinding.release_instances(
        [*released, *plan.reassigned],
        reason=f"租户实例归属变更: tenant={tenant.name}",
    )

    logger.info(f"更新租户实例绑定: tenant={tenant.name}, instances={wanted}, 域绑定释放={dropped}")
    if released:
        logger.warning(f"租户实例解绑: tenant={tenant.name}, released={released}")
    return await describe_tenant(tenant)


@router.post("/tenants/{tenant_id}/instances/preview", summary="预览租户实例划转")
async def preview_tenant_instances(
    tenant_id: int,
    body: TenantInstanceBindRequest,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> TenantInstanceTransferPreview:
    """预览这次保存会改变哪些实例的归属、各自带走多少记忆，仅平台方可用

    校验与保存完全相同（租户不存在 404、缺字段 400、重复 400、实例不存在 404、已归属
    别的租户 400），但**不写任何东西**：不记令牌曝光、不记工作区标记、不改归属、不释放域。
    `clear_memory_and_affection` 在预览里不生效，它只决定保存时要不要真删。
    """
    from nekro_agent.services.instance_settings import count_instance_settings
    from nekro_agent.services.memory.admin_service import count_instance_memory

    plan = await _plan_instance_binding(tenant_id, body)
    current_tenant_name = plan.tenant.display_name or plan.tenant.name
    changes: List[TenantInstanceTransferChange] = []
    for row, is_released in plan.changed_rows:
        counts = await count_instance_memory(row.adapter_key, row.instance_key)
        email_accounts, email_user_prefs = await _count_instance_mailboxes(row.adapter_key, row.instance_key)
        # `release_instances` 按 instance_key 批量删绑定行：只要这一行自己有绑定行，
        # 它的 key 就在 released/reassigned 里，保存后一定会被移出实例组。
        binding = await DBInstanceDomainBinding.get_binding(row.adapter_key, row.instance_key)
        settings = await count_instance_settings(row.adapter_key, row.instance_key)
        changes.append(
            TenantInstanceTransferChange(
                adapter_key=row.adapter_key,
                instance_key=row.instance_key,
                display_name=row.display_name or row.instance_key,
                from_tenant_id=plan.tenant.id if is_released else None,
                from_tenant_name=current_tenant_name if is_released else "",
                to_tenant_id=None if is_released else plan.tenant.id,
                to_tenant_name="" if is_released else current_tenant_name,
                in_group=binding is not None,
                memory_paragraphs=counts.paragraphs,
                memory_quarantined=counts.quarantined,
                memory_summaries=counts.summaries,
                memory_prompt_rounds=counts.prompt_rounds,
                worldbook_books=counts.worldbook_books,
                worldbook_entries=counts.worldbook_entries,
                memory_affection_rows=counts.affection_rows,
                memory_affection_events=counts.affection_events,
                collected_emotions=(await count_instance_emotions(row.adapter_key, row.instance_key))["items"],
                channel_quota_tenant_settings=await _count_tenant_quota_settings(row.adapter_key, row.instance_key),
                ssh_credentials=await _count_instance_ssh_credentials(row.adapter_key, row.instance_key),
                email_accounts=email_accounts,
                email_user_prefs=email_user_prefs,
                plugin_settings=settings.plugin_settings,
                config_overrides=settings.config_overrides,
            ),
        )
    changes.sort(key=lambda change: (change.instance_key, change.adapter_key))
    return TenantInstanceTransferPreview(changes=changes)
