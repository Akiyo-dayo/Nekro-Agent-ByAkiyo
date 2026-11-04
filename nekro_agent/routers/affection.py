"""好感度管理接口

## 作用域

好感度按 **(域, 人, 人设)** 隔离（见 `services/identity_scope.py`），因此所有查询都以
实例作用域为隐式过滤条件：

- 平台方传空 `instance_key` 表示全局视图，看到全部实例的数据；
- 租户必须显式指定自己名下的实例，空串对他们不能退化成全局视图，
  传别人的实例标识按 **404** 处理（见 `assert_instance_accessible` 的存在性掩盖约定）。

**作用域必须先换算成域再过滤，不能直接拿 `instance_key` 当过滤条件。** 实例被放进实例组
之后，它的好感行落在 `(adapter_key="", instance_key="", domain_group_id=组ID)` 上——
按实例列去查是**一行都查不到**（fail-closed）。表现为"配了域之后后台好感度整个空了"，
而数据其实好好地在库里。换算见 `store.affection_domain_of_instance`。

## 一行属于哪一份好感，界面上必须说得出来

同一个人在群 A（人设甲）80 分、群 B（人设乙）0 分是**设计意图**（"换了个人设就不会记得
以前的恩怨情仇"），但没有任何标注的话，运营只会把它当成数据丢了。因此列表与详情都带上
`domain_id` / `preset_id` / `preset_name`：这一份好感属于哪个域、哪个人设。

## 一行的 `platform_userid` 是「人」，不是「账号」

合并过的账号（换号）在库里仍然各有各的行，只是**副账号那一行不再被写入**——
它是一份休眠的历史数据，删掉别名就会立刻复活。列表用 `merged_into` 把这种行标出来，
否则管理员会对着一个"分数永远不动"的行反复调整。事件流水则按人取并集（`store.list_events`），
每条都带上真正的来源账号。

## 一行对应「某人在人设 X 下的那一份」

同一个人换过人设就有好几行。列表因此带上 `preset_name` 与 `in_use_channels`
（本域里当前生效人设正是这一行人设的频道数，0 = 眼下没有频道在用这一份）。
「当前生效人设」只认 `DBChatChannel.get_effective_preset_id` 那一条回退链
（经 `identity_scope.resolve_preset_id`），这里不另写一套。

## 后台能做什么

- 调分（`adjust`）与重置（`reset`，分数归零 + 清两段描述）；
- 只清印象（`clear-narrative`）：清两段描述，分数不动；
- 编辑印象（`narrative`）：改写两段描述，去 NUL、按上限截断，注入时照旧按不可信块包裹；
- 删一条流水的理由（`events/{event_id}`）：软删，只删文字、留删除痕迹，分数与 `score_after` 不动。
  只在这一行那个人的流水里找（同域、同人设、含合并过来的账号），与详情页时间线同一个范围。

后面三项都只记宿主固定文案，不把新旧文字抄进流水；做完当场让快照缓存失效，下一轮提示词
就是新内容；评估只读这之后的对话、进行中的评估遇上它作废（见 `services/affection/store.py`）。
所有写操作与读操作同档：`Role.Admin`，先 `resolve_instance_scope` 再 `_get_visible_record`，
越权一律 404（不暴露记录在不在）。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import SELF_DOMAIN_GROUP_ID, DBInstanceDomainBinding
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services.affection.merge import list_person_accounts
from nekro_agent.services.affection.stage import (
    AFFECTION_MAX_SCORE,
    AFFECTION_MIN_SCORE,
    AffectionEventSource,
    AffectionStage,
    all_stages,
    stage_label,
    stage_of,
)
from nekro_agent.services.affection.store import (
    REASON_NARRATIVE_CLEAR,
    affection_domain_of_instance,
    apply_delta,
    clear_narrative,
    edit_narrative,
    key_of_record,
    list_events,
    redact_event_reason,
    set_score,
)
from nekro_agent.services.identity_admin import (
    domain_label,
    group_names,
    preset_display_name,
    preset_names,
)
from nekro_agent.services.identity_scope import ScopeDomain, resolve_affection_domain, resolve_preset_id
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import InstanceRef, assert_instance_accessible, get_tenant_scope

router = APIRouter(prefix="/affection", tags=["Affection"])

logger = get_sub_logger("affection")

INSTANCE_SCOPE_DESC = "实例作用域标识；留空表示平台方全局视图（可见全部实例的好感度）"

# 人工增减的幅度上限。取整个取值区间的跨度：任何合法的目标值都能一步到位，
# 同时挡住 JSON 里传进来的天文数字。
MAX_MANUAL_DELTA = AFFECTION_MAX_SCORE - AFFECTION_MIN_SCORE


class AffectionStageInfo(BaseModel):
    value: str
    label: str


class AffectionItem(BaseModel):
    id: int
    adapter_key: str
    instance_key: str
    # === 隔离键里"域"与"人设"两维的原始取值 ===
    # 三列一组的域编码（见 `models/db_affection.py`）。`domain_group_id > 0` 时
    # `adapter_key` / `instance_key` 恒为空串——域是实例组，不是任何一个成员实例。
    # 前端据此渲染「这份好感属于：域 X / 人设 甲」，可读标签在详情页的 `scope` 里。
    domain_group_id: int = 0
    domain_id: str = ""
    preset_id: int = 0
    platform_userid: str
    nickname: str
    score: int
    stage: str
    stage_label: str
    state_text: str
    last_event_text: str
    interaction_count: int
    last_chat_key: str
    last_interact_at: Optional[str] = None
    update_time: Optional[str] = None
    # 本行的账号已经被合并到哪个人名下；空串 = 没被合并过（绝大多数行）。
    # 非空表示这是一份**休眠**数据：Bot 读的是 `merged_into` 那个人的行，
    # 在这一行上调分数不会有任何效果。不标出来的话管理员会对着一个
    # "改了也不动"的行反复调整（删掉别名它就会立刻复活）。
    merged_into: str = ""
    # 这一行属于哪个人设（可读名字）。同一个人换过人设就有好几行，不标出来的话只差分数和描述，
    # 运营会把重置点在一行没人用的旧人设上（现网真实发生过）。
    preset_name: str = ""
    # 本域里当前生效人设正是这一行人设的频道数；0 = 眼下没有任何频道在用这一份
    in_use_channels: int = 0


class AffectionListResponse(BaseModel):
    total: int
    items: List[AffectionItem]
    stages: List[AffectionStageInfo]


class AffectionEventItem(BaseModel):
    id: int
    delta: int
    score_after: int
    source: str
    reason: str
    operator: str
    chat_key: str
    # 触发这条变化的**原始账号**。时间线是按人取的并集，合并过的人身上会同时出现
    # 几个号干的事；不标出来的话"他明明没说话怎么涨分了"没法解释。
    # 没合并过的人这里恒等于本行的人标识（库里存空串表示"与人相同"，见 db_affection.py）。
    source_platform_userid: str = ""
    create_time: Optional[str] = None
    # 理由被管理员删掉了：`reason` 恒为空串，只给删除痕迹（谁、什么时候）
    deleted: bool = False
    deleted_by: str = ""
    deleted_at: Optional[str] = None


class AffectionScopeInfo(BaseModel):
    """这份好感**属于谁**：域 X / 人设 甲

    没有这一段，「为什么他在这个群 80 分那个群 0 分」没人解释得清 ——
    两个群绑着不同人设时那就是两份好感，不是数据丢了。
    `person_uid` 与 `alias_accounts` 是同一件事的另一半：这一行记的是"人"，
    而不是"某个账号"，被合并进来的副账号列在 `alias_accounts` 里。
    """

    domain_id: str
    domain_label: str
    is_group_domain: bool
    preset_id: int
    preset_name: str
    # 人设行已被删除时为 False：数据保留成孤儿，不级联删（人设可能是误删）
    preset_exists: bool
    person_uid: str
    alias_accounts: List[str] = Field(default_factory=list)


class AffectionDetail(BaseModel):
    item: AffectionItem
    events: List[AffectionEventItem]
    scope: AffectionScopeInfo


class AffectionSummary(BaseModel):
    total: int
    stage_counts: List["AffectionStageCount"]
    top_score: int


class AffectionStageCount(BaseModel):
    value: str
    label: str
    count: int


class AffectionAdjustRequest(BaseModel):
    """人工调整请求

    `delta` 与 `score` 二选一：前者是"在现有基础上加减"，后者是"直接设成这个值"。
    两个都不传视为无效请求，同时传以 `score` 为准。
    """

    delta: Optional[int] = None
    score: Optional[int] = None
    reason: str = Field(default="", max_length=200)


class AffectionNarrativeEditRequest(BaseModel):
    """编辑印象请求

    不在这里卡长度：超长按两列各自的上限截断（`store.edit_narrative`），而不是整条打回——
    管理员贴一段稍长的文字时，截掉尾巴比报错再重填友好。
    """

    state_text: str = ""
    last_event_text: str = ""


class ActionResponse(BaseModel):
    ok: bool = True


def _format_dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _domain_of(record: DBAffection) -> ScopeDomain:
    """把好感行上的域三列读回一个 `ScopeDomain`

    **不要在别处自己拼这三列**：`ScopeDomain` 是这套编码的唯一产生者，
    读侧多一份等价实现，两份就会在下一次改判据时分叉（本项目已经栽过好几次）。
    因此这里也不自己判"是组还是自成一域"——那条判据在 `AffectionKey.domain` 上有一份，
    整行读回键再取域，判据仍然只有一处。
    """
    return key_of_record(record).domain


async def _merged_into_map(records: Sequence[DBAffection]) -> Dict[Tuple[str, str], str]:
    """批量查出「这一行的账号是不是某个人的副号」，键是 `(域, 账号)`

    一页最多 200 行，逐行点查会变成 200 次往返，因此合并成一次 `IN`。

    **域这一维靠的是「键」而不是「过滤条件」**：返回的映射以 `(别名行自己的域, 账号)` 为键，
    调用方也拿 `(本行的域, 本行的账号)` 去取，因此另一个域的别名行即便被查出来也落在
    另一个键上，永远贴不到本页的行上。`domain_id__in` 只是**收窄**——变异掉它不会有用例
    变红（已核实的等价变异），但删掉它会让平台方全局视图多捞回一批用不上的行。
    平台方一页里确实会同时出现好几个域的行，所以既不能只取第一行的域，
    也不能把键收缩成只有账号。
    """
    if not records:
        return {}
    domain_ids = {_domain_of(record).domain_id for record in records}
    accounts = {record.platform_userid for record in records}
    rows = await DBIdentityAlias.filter(
        domain_id__in=sorted(domain_ids),
        platform_userid__in=sorted(accounts),
    ).order_by("id").values_list("domain_id", "platform_userid", "person_uid")
    # 唯一约束保证 (域, 账号) 只有一行；脏行下取最早那行，与 `identity_scope` 的读侧取舍一致
    merged: Dict[Tuple[str, str], str] = {}
    for domain_id, platform_userid, person_uid in rows:
        merged.setdefault((str(domain_id), str(platform_userid)), str(person_uid))
    return merged


async def _in_use_channel_counts(records: Sequence[DBAffection]) -> Dict[Tuple[str, int], int]:
    """每一行「本域里当前生效人设正是这一行人设」的频道数，键是 `(域, 人设)`

    **只数这一行自己那个域里的频道**：租户作用域下整页都是同一个域（作用域换算出来的那个），
    平台方全局视图里每一行各数各的域——别的域、别的租户的频道一个都不进来。

    域里有哪些频道：组域取「好感域指向本组」的成员实例（`members_of_affection_group`），
    自成一域就是那一个实例；每个实例再用唯一判据 `resolve_affection_domain` 复核一次它眼下
    确实在这个域里（实例进组之后，它留在自成一域上的旧行就不该再算任何频道在用）。
    每个频道的生效人设一律走 `resolve_preset_id`（即 `DBChatChannel.get_effective_preset_id`
    那条回退链），不在这里另写一套——那正是本项目反复栽跟头的「同一判据的第二处实现」。

    代价是每个频道最多三次主键点查；同一页里一个域只算一次。
    """
    domains: Dict[str, ScopeDomain] = {}
    for record in records:
        domain = _domain_of(record)
        domains.setdefault(domain.domain_id, domain)

    counts: Dict[Tuple[str, int], int] = {}
    for domain_id, domain in domains.items():
        if domain.is_group:
            members = await DBInstanceDomainBinding.members_of_affection_group(domain.group_id)
        else:
            members = [(domain.adapter_key, domain.instance_key)]
        per_preset: Counter[int] = Counter()
        for adapter_key, instance_key in members:
            if await resolve_affection_domain(adapter_key, instance_key) != domain:
                continue
            for channel in await DBChatChannel.filter(adapter_key=adapter_key, instance_key=instance_key):
                per_preset[await resolve_preset_id(channel)] += 1
        for preset_id, count in per_preset.items():
            counts[(domain_id, preset_id)] = count
    return counts


async def _items_of(records: Sequence[DBAffection]) -> List[AffectionItem]:
    """把一页记录转成接口条目：合并标记、人设名、在用频道数一并批量算好"""
    merged = await _merged_into_map(records)
    names = await preset_names(sorted({record.preset_id for record in records}))
    in_use = await _in_use_channel_counts(records)
    return [
        _to_item(
            record,
            merged,
            preset_name=preset_display_name(record.preset_id, names),
            in_use_channels=in_use.get((_domain_of(record).domain_id, record.preset_id), 0),
        )
        for record in records
    ]


def _to_item(
    record: DBAffection,
    merged_into: Dict[Tuple[str, str], str] | None = None,
    *,
    preset_name: str = "",
    in_use_channels: int = 0,
) -> AffectionItem:
    stage = stage_of(record.score)
    return AffectionItem(
        merged_into=(merged_into or {}).get((_domain_of(record).domain_id, record.platform_userid), ""),
        preset_name=preset_name,
        in_use_channels=in_use_channels,
        id=record.id,
        adapter_key=record.adapter_key,
        instance_key=record.instance_key,
        domain_group_id=record.domain_group_id,
        domain_id=_domain_of(record).domain_id,
        preset_id=record.preset_id,
        platform_userid=record.platform_userid,
        nickname=record.nickname,
        score=record.score,
        stage=stage.value,
        stage_label=stage_label(stage),
        state_text=record.state_text,
        last_event_text=record.last_event_text,
        interaction_count=record.interaction_count,
        last_chat_key=record.last_chat_key,
        last_interact_at=_format_dt(record.last_interact_at),
        update_time=_format_dt(record.update_time),
    )


def _to_event(event: DBAffectionEvent) -> AffectionEventItem:
    deleted = event.deleted_at is not None
    return AffectionEventItem(
        id=event.id,
        delta=event.delta,
        score_after=event.score_after,
        source=event.source,
        # 删过的条目不给理由：库里已经置空，这里再兜一层，免得哪天有人绕过删除入口直接改列
        reason="" if deleted else event.reason,
        operator=event.operator,
        chat_key=event.chat_key,
        # 空串在库里表示"与 platform_userid 相同"（存量行与没合并过的人都是这个取值），
        # 展示时还原成具体账号，前端不必再写一次这条回落规则
        source_platform_userid=event.source_platform_userid or event.platform_userid,
        create_time=_format_dt(event.create_time),
        deleted=deleted,
        deleted_by=event.deleted_by if deleted else "",
        deleted_at=_format_dt(event.deleted_at),
    )


async def resolve_instance_scope(user: DBUser, instance_key: str) -> InstanceRef:
    """把请求里的实例作用域参数解析成当前身份实际可用的作用域

    Returns:
        `(adapter_key, instance_key)`。平台方原样透传标识（空串即全局视图）、适配器留空；
        租户拿到的是归属守卫认定的**那一行**的适配器。

    租户必须指定自己名下的实例，传他人的实例标识按 404 处理，不暴露该实例是否存在。
    适配器要一并带下去：另一个适配器下可以有归别人的同名实例，只凭标识换算好感域会换到那一边。
    """
    scope = instance_key.strip()
    if get_tenant_scope(user) is None:
        return "", scope
    if not scope:
        raise ValidationError(reason="租户身份必须指定实例作用域")
    instance = await assert_instance_accessible(user, scope)
    return instance.adapter_key, scope


async def _scoped_query(scope: str, adapter_key: str = "") -> QuerySet[DBAffection]:
    """按实例作用域筛出可见的好感度记录

    **作用域是实例，可见范围却是域**。实例被放进实例组之后它的好感行落在
    `(adapter_key="", instance_key="", domain_group_id=组ID)` 上，按 `instance_key=scope`
    过滤会一行都查不到——后台整页空白，而数据其实在库里。所以这里先把实例换算成域，
    再用 `ScopeDomain.as_columns()`（域编码的唯一产生者）去过滤。

    同一个组里的几个实例因此互相看得见对方的好感——**那正是"放进同一个域"的定义**。
    跨租户不会因此串门：入组时 `DBInstanceGroup.assert_instance_joinable` 要求成员与组的
    租户归属逐字相等，归属事后变化时 `release_instances` 会把绑定作废。

    实例行查不到时退化成按实例列过滤，也就是与今天逐字相同的行为，
    理由见 `store.affection_domain_of_instance` 的 docstring。回退分支上那个
    `domain_group_id=SELF_DOMAIN_GROUP_ID` 是**冗余的**（组域的行 `instance_key` 恒为空串，
    而走到这里时 `scope` 非空，两者不可能同时成立），因此变异掉它不会有用例变红——
    这是一个已核实的等价变异，不是覆盖缺口。留着是因为它把"这条分支只看自成一域"
    写成了显式条件，而不是让读者去推那条蕴含关系。
    """
    if not scope:
        return DBAffection.all()
    domain = await affection_domain_of_instance(scope, adapter_key=adapter_key)
    if domain is None:
        return DBAffection.filter(instance_key=scope, domain_group_id=SELF_DOMAIN_GROUP_ID)
    return DBAffection.filter(**domain.as_columns())


async def _get_visible_record(record_id: int, scope: str, adapter_key: str = "") -> DBAffection:
    """取一条对该作用域可见的记录，越权一律按不存在处理"""
    record = await (await _scoped_query(scope, adapter_key)).filter(id=record_id).first()
    if record is None:
        raise NotFoundError(resource="好感度记录")
    return record


@router.get("/list", summary="获取好感度列表", response_model=AffectionListResponse)
@require_role(Role.Admin)
async def get_affection_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    search: str = Query(default=""),
    stage: Optional[AffectionStage] = Query(default=None),
    sort_by: str = Query(default="score_desc"),
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionListResponse:
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    query = await _scoped_query(scope, adapter_key)

    keyword = search.strip()
    if keyword:
        query = query.filter(Q(nickname__icontains=keyword) | Q(platform_userid__icontains=keyword))
    if stage is not None:
        query = query.filter(stage=stage.value)

    total = await query.count()
    order = {
        "score_asc": "score",
        "score_desc": "-score",
        "recent": "-update_time",
        "interaction": "-interaction_count",
    }.get(sort_by, "-score")
    records = await query.order_by(order, "-id").offset((page - 1) * page_size).limit(page_size)

    return AffectionListResponse(
        total=total,
        items=await _items_of(records),
        stages=[AffectionStageInfo(value=item.value, label=stage_label(item)) for item in all_stages()],
    )


@router.get("/summary", summary="获取好感度分布概览", response_model=AffectionSummary)
@require_role(Role.Admin)
async def get_affection_summary(
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionSummary:
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    query = await _scoped_query(scope, adapter_key)

    total = await query.count()
    counts = {stage.value: 0 for stage in all_stages()}
    top_score = 0
    for score, record_stage in await query.values_list("score", "stage"):
        if record_stage in counts:
            counts[record_stage] += 1
        top_score = max(top_score, int(score))

    return AffectionSummary(
        total=total,
        stage_counts=[
            AffectionStageCount(value=item.value, label=stage_label(item), count=counts[item.value])
            for item in all_stages()
        ],
        top_score=top_score,
    )


@router.get("/{record_id}", summary="获取好感度详情", response_model=AffectionDetail)
@require_role(Role.Admin)
async def get_affection_detail(
    record_id: int,
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionDetail:
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    record = await _get_visible_record(record_id, scope, adapter_key)
    # 流水按**人**取并集（合并过的副账号那些行也在里面），来源账号逐条标在 `_to_event` 上
    events = await list_events(key_of_record(record), limit=30)
    return AffectionDetail(
        item=(await _items_of([record]))[0],
        events=[_to_event(event) for event in events],
        scope=await _scope_info(record),
    )


async def _scope_info(record: DBAffection) -> AffectionScopeInfo:
    """组装「这份好感属于：域 X / 人设 甲」

    标签的措辞与后台「身份与域」页共用 `identity_admin` 的那一份，
    不要在这里另写一套——同一个域在两个页面上叫两个名字，运营会以为是两个东西。
    """
    domain = _domain_of(record)
    names = await preset_names([record.preset_id])
    # 「这个人有哪些账号」的判据只有 `merge.list_person_accounts` 一份（主账号在前）。
    # 在这里内联一遍 `DBIdentityAlias.filter(...)` 就是同一个判据的第二处实现——
    # 而这个判据的第一维是**域**，写漏就是把另一个租户的合并关系列进本页。
    aliases = (await list_person_accounts(domain, record.platform_userid))[1:]
    return AffectionScopeInfo(
        domain_id=domain.domain_id,
        domain_label=domain_label(domain, (await group_names([domain.group_id])).get(domain.group_id, "")),
        is_group_domain=domain.is_group,
        preset_id=record.preset_id,
        preset_name=preset_display_name(record.preset_id, names),
        preset_exists=record.preset_id <= 0 or record.preset_id in names,
        person_uid=record.platform_userid,
        alias_accounts=list(aliases),
    )


@router.post("/{record_id}/adjust", summary="人工调整好感度", response_model=AffectionItem)
@require_role(Role.Admin)
async def adjust_affection(
    record_id: int,
    body: AffectionAdjustRequest,
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionItem:
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    record = await _get_visible_record(record_id, scope, adapter_key)
    key = key_of_record(record)
    reason = body.reason.strip() or "管理员手动调整"

    if body.score is not None:
        snapshot = await set_score(
            key,
            score=body.score,
            reason=reason,
            source=AffectionEventSource.MANUAL,
            operator=_current_user.username,
        )
    elif body.delta is not None:
        snapshot = await apply_delta(
            key,
            # 人工调整不受自动评估那条"单轮最多涨几点"的约束：管理员是来修正数据的。
            # 这里只挡住整型溢出，最终值仍由 `clamp_score` 收进合法区间。
            delta=max(-MAX_MANUAL_DELTA, min(MAX_MANUAL_DELTA, body.delta)),
            reason=reason,
            source=AffectionEventSource.MANUAL,
            operator=_current_user.username,
            bump_interaction=False,
        )
    else:
        raise ValidationError(reason="必须提供 delta 或 score")

    logger.info(
        f"好感度人工调整: operator={_current_user.username}, key={key}, score={snapshot.score}",
    )
    refreshed = await _get_visible_record(record_id, scope, adapter_key)
    return (await _items_of([refreshed]))[0]


@router.post("/{record_id}/reset", summary="重置好感度", response_model=AffectionItem)
@require_role(Role.Admin)
async def reset_affection(
    record_id: int,
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionItem:
    """把分数归零并清空两段描述

    只想抹掉不合适的描述、不想动分数时用 `clear-narrative`。
    """
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    record = await _get_visible_record(record_id, scope, adapter_key)
    await set_score(
        key_of_record(record),
        score=0,
        reason="管理员重置",
        source=AffectionEventSource.RESET,
        operator=_current_user.username,
        clear_state_text=True,
    )
    logger.info(f"好感度重置: operator={_current_user.username}, record_id={record_id}")
    refreshed = await _get_visible_record(record_id, scope, adapter_key)
    return (await _items_of([refreshed]))[0]


@router.post("/{record_id}/clear-narrative", summary="只清印象（保留分数）", response_model=AffectionItem)
@require_role(Role.Admin)
async def clear_affection_narrative(
    record_id: int,
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionItem:
    """清空两段描述（`state_text` / `last_event_text`），分数、阶段、互动次数都不动"""
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    record = await _get_visible_record(record_id, scope, adapter_key)
    await clear_narrative(
        key_of_record(record),
        source=AffectionEventSource.NARRATIVE_CLEAR,
        reason=REASON_NARRATIVE_CLEAR,
        operator=_current_user.username,
    )
    logger.info(f"好感度只清印象: operator={_current_user.username}, record_id={record_id}")
    refreshed = await _get_visible_record(record_id, scope, adapter_key)
    return (await _items_of([refreshed]))[0]


@router.put("/{record_id}/narrative", summary="编辑印象", response_model=AffectionItem)
@require_role(Role.Admin)
async def edit_affection_narrative(
    record_id: int,
    body: AffectionNarrativeEditRequest,
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionItem:
    """改写两段描述，分数不动；流水只记宿主固定文案，不抄新旧文字（日志同样不打）"""
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    record = await _get_visible_record(record_id, scope, adapter_key)
    await edit_narrative(
        key_of_record(record),
        state_text=body.state_text,
        last_event_text=body.last_event_text,
        operator=_current_user.username,
    )
    logger.info(f"好感度编辑印象: operator={_current_user.username}, record_id={record_id}")
    refreshed = await _get_visible_record(record_id, scope, adapter_key)
    return (await _items_of([refreshed]))[0]


@router.delete(
    "/{record_id}/events/{event_id}",
    summary="删除一条流水的理由（软删，分数不动）",
    response_model=AffectionEventItem,
)
@require_role(Role.Admin)
async def delete_affection_event_reason(
    record_id: int,
    event_id: int,
    instance_key: str = Query(default="", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AffectionEventItem:
    """只删这一条的理由文字：`reason` 置空，留下删除时间与操作者；`delta` / `score_after` 与行都不动

    **范围就是详情页时间线的范围**：先按作用域取到这一行（越权 404），再只在这一行对应的那个人
    （同域、同人设、含合并过来的账号）的流水里按 `event_id` 找，找不到同样 404。
    不能只凭 `event_id` 去删：流水编号是全表自增的，可以被枚举到别的租户、别的人头上。
    """
    adapter_key, scope = await resolve_instance_scope(_current_user, instance_key)
    record = await _get_visible_record(record_id, scope, adapter_key)
    event = await redact_event_reason(key_of_record(record), event_id, operator=_current_user.username)
    if event is None:
        raise NotFoundError(resource="好感度流水")
    logger.info(
        f"好感度流水理由已删除: operator={_current_user.username}, record_id={record_id}, event_id={event_id}",
    )
    return _to_event(event)


AffectionSummary.model_rebuild()
