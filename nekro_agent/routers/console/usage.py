"""LLM 用量与费用（总后台）

## 这一族回答什么问题

「**哪个租户/哪个实例**、用**哪个模型组/模型**、烧了多少 **prompt/completion token**、
折算多少**钱**、**什么时候**」——以及"失败/重试/fallback 各占多少"。
账本本身在 `models/db_llm_usage.py`，写侧在 `services/usage/ledger.py`。

⚠️ **不回答「哪个人」**：`person_uid` 这一列当前没有任何生产者，理由见
`models/db_llm_usage.py` 的模块文档（计费单元不是人，一次回合的 prompt 是整个频道的上下文）。
出参里保留这一列是为了不让将来接上时再改一次契约，但**页面不要拿它当维度画**。

## 权限模型

读接口一律 `Role.Admin`：定制版客户要能在自己的后台看自己的账，
这与 `console/quota.py` 是同一条理由（"可见范围由租户作用域而不是 `Role` 决定"）。
作用域全部收进 `services/usage/query.UsageScope`，端点里不出现裸的
`DBLLMUsageRecord.filter(...)`；越权一律 **404**（`assert_instance_accessible` /
`assert_tenant_accessible`）。

平台方看全部；租户只看得到 `tenant_id` 等于自己的那些行，因此
`tenant_id IS NULL` 的那些（平台方自己的插件生成、连通性测试、以及归不了属的
`UNATTRIBUTED`）对租户天然不可见。

**模型组名对租户按 `can_view_group` 掩码**，理由见 `services/usage/query.py` 的模块文档。

## 只做账本 + 视图，不做扣费

本模块只记账、只出视图，超出配额之后怎么处置不在这一层。这里连"剩余额度"都不算，
免得先斩后奏地固化一个口径。
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.usage import LLMUsageScene, LLMUsageStatus
from nekro_agent.services.usage.query import (
    DEFAULT_WINDOW_DAYS,
    MAX_TREND_BUCKETS,
    MAX_WINDOW_DAYS,
    aggregate_by,
    aggregate_totals,
    aggregate_trend,
    resolve_tenant_labels,
    resolve_usage_scope,
    visible_model_group_names,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(tags=["Console"])

INSTANCE_DESC = "实例作用域；留空表示当前身份可见的全部实例"
TENANT_DESC = "租户作用域；留空时平台方表示全部租户，租户身份恒为自己"
START_DESC = f"起始时间（ISO 8601，含）；留空表示 {DEFAULT_WINDOW_DAYS} 天前"
END_DESC = "结束时间（ISO 8601，不含）；留空表示此刻"


class UsageMetrics(BaseModel):
    """一组聚合出来的量。四个数值列在每一档里都是同一个口径"""

    records: int = Field(description="请求条数（含失败与中断）")
    token_input: int = Field(description="输入 token 合计")
    token_output: int = Field(description="输出 token 合计")
    token_total: int = Field(description="上游给出的总 token 合计")
    cost: float = Field(description="按当时倍率折算的费用合计")


class UsageBreakdownItem(UsageMetrics):
    key: str = Field(description="该分档的取值；空串表示该维度缺失")


class UsageSummaryResponse(BaseModel):
    start_time: str
    end_time: str
    total: UsageMetrics
    by_instance: List[UsageBreakdownItem]
    by_tenant: List[UsageBreakdownItem]
    by_model_group: List[UsageBreakdownItem]
    by_model: List[UsageBreakdownItem]
    by_scene: List[UsageBreakdownItem]
    by_status: List[UsageBreakdownItem]


class UsageTrendPoint(UsageMetrics):
    bucket_start: str


class UsageTrendResponse(BaseModel):
    start_time: str
    end_time: str
    bucket_hours: int
    points: List[UsageTrendPoint]


class UsageRecordItem(BaseModel):
    id: int
    create_time: str
    adapter_key: str
    instance_key: str
    domain_id: str
    tenant_id: Optional[int]
    chat_key: str
    person_uid: str
    preset_id: int
    model_group_name: str
    model_name: str
    stream_mode: bool
    token_input: int
    token_output: int
    token_total: int
    cost: float
    scene: str
    status: str
    error_type: str
    finish_reason: str
    attempt_index: int
    attempt_total: int
    is_fallback: bool
    generation_time_ms: int


class UsageRecordListResponse(BaseModel):
    total: int
    items: List[UsageRecordItem]


class UsageEnumsResponse(BaseModel):
    """给前端渲染筛选器用的固定取值表，避免前端自己抄一份枚举"""

    scenes: List[str]
    statuses: List[str]
    max_window_days: int
    max_trend_buckets: int


def _to_metrics(row: Dict[str, float]) -> UsageMetrics:
    return UsageMetrics(
        records=int(row["records"]),
        token_input=int(row["token_input"]),
        token_output=int(row["token_output"]),
        token_total=int(row["token_total"]),
        cost=float(row["cost"]),
    )


def _to_items(
    rows: List[Dict[str, object]],
    *,
    alias: Optional[Dict[str, str]] = None,
    merge_aliased: bool = False,
) -> List[UsageBreakdownItem]:
    """把聚合行转成出参；`alias` 用于把模型组名换成本身份该看到的字符串

    掩码在**出参转换这一层**做，而不是在聚合之后再遍历一遍：分组的 key 与列表项的
    `model_group_name` 走同一个映射，两处各写一遍就会出现"图表里掩了、表格里没掩"。

    ## `merge_aliased`：掩码**必然**制造同名行，不合并就是一张读不懂的账单

    `visible_model_group_names` 把租户看不见的**每一个**平台组都换成同一个
    `HIDDEN_MODEL_GROUP_LABEL`。平台方配两个组（这是常态：一个主组一个 fallback），
    租户的账单上就会出现**两行一模一样**的「(未公开的模型组)」——数字不同、标签相同，
    "这两行差在哪"无解；前端拿 `key` 当 React 列表键还会撞。

    这与 `resolve_tenant_labels` 的撞名问题是**同一个形态**，但解法必须相反：
    租户那边可以退回唯一的 `name` 把两行区分开，而这边"区分开"恰恰是掩码要禁止的事
    （能数出平台方有几个组、各花了多少，就已经泄露了那份清单的规模）。
    所以这里**合并**：一行「(未公开的模型组)」，数字是它们的合计。

    只对模型组那一档开启。租户/实例/模型/场景那几档的 key 天然互不相同，
    对它们合并是无谓的循环。
    """
    items: List[UsageBreakdownItem] = []
    # key → 它在 `items` 里的下标。**不要用 `items.index(...)` 回查**：
    # `UsageBreakdownItem` 是 pydantic 模型，`list.index` 走的是值相等而不是同一性，
    # 两行恰好每一列都相同时会改到前面那一行去（本仓在"比较两个恰好相同的清单"上栽过）。
    position: Dict[str, int] = {}
    for row in rows:
        raw_key = str(row["key"])
        metrics = _to_metrics(row)  # type: ignore[arg-type]
        key = alias.get(raw_key, raw_key) if alias else raw_key
        item = UsageBreakdownItem(key=key, **metrics.model_dump())
        if not merge_aliased or key not in position:
            if merge_aliased:
                position[key] = len(items)
            items.append(item)
            continue
        # 合并进已有的那一行；四个数值列是同一口径，逐列相加即可
        index = position[key]
        existing = items[index]
        items[index] = UsageBreakdownItem(
            key=key,
            records=existing.records + item.records,
            token_input=existing.token_input + item.token_input,
            token_output=existing.token_output + item.token_output,
            token_total=existing.token_total + item.token_total,
            cost=existing.cost + item.cost,
        )
    if merge_aliased:
        # 合并会打乱"按费用从高到低"（`aggregate_by` 的排序），重排一次
        items.sort(key=lambda entry: entry.cost, reverse=True)
    return items


@router.get("/usage/enums", summary="用量筛选器的固定取值")
@require_role(Role.Admin)
async def get_usage_enums(
    _current_user: DBUser = Depends(get_current_active_user),
) -> UsageEnumsResponse:
    """纯常量，不查库也不带作用域——它不暴露任何一行数据"""
    return UsageEnumsResponse(
        scenes=[scene.value for scene in LLMUsageScene],
        statuses=[status.value for status in LLMUsageStatus],
        max_window_days=MAX_WINDOW_DAYS,
        max_trend_buckets=MAX_TREND_BUCKETS,
    )


@router.get("/usage/summary", summary="用量与费用汇总")
@require_role(Role.Admin)
async def get_usage_summary(
    instance_key: str = Query(default="", description=INSTANCE_DESC),
    tenant_id: Optional[int] = Query(default=None, description=TENANT_DESC),
    start_time: Optional[str] = Query(default=None, description=START_DESC),
    end_time: Optional[str] = Query(default=None, description=END_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> UsageSummaryResponse:
    """按实例 / 租户 / 模型组 / 模型 / 场景 / 收尾形态六个维度分档汇总"""
    scope = await resolve_usage_scope(
        _current_user,
        instance_key=instance_key,
        tenant_id=tenant_id,
        start_time=start_time,
        end_time=end_time,
    )
    total = await aggregate_totals(scope)
    by_instance = await aggregate_by(scope, "instance_key")
    by_tenant = await aggregate_by(scope, "tenant_id")
    by_model_group = await aggregate_by(scope, "model_group_name")
    by_model = await aggregate_by(scope, "model_name")
    by_scene = await aggregate_by(scope, "scene")
    by_status = await aggregate_by(scope, "status")

    alias = await visible_model_group_names(_current_user, [str(row["key"]) for row in by_model_group])
    # 「每个客户烧了多少」是这本账要回答的第一个问题，而分组键是裸的 `tenant_id`。
    # 换成名字在**出参这一层**做，与模型组名同一处（见 `_to_items` 的说明）。
    tenant_alias = await resolve_tenant_labels(_current_user, [str(row["key"]) for row in by_tenant])
    return UsageSummaryResponse(
        start_time=scope.start_time.isoformat(),
        end_time=scope.end_time.isoformat(),
        total=_to_metrics(total),
        by_instance=_to_items(by_instance),
        by_tenant=_to_items(by_tenant, alias=tenant_alias),
        # 掩码把多个平台组折成同一个标签，不合并就是若干行同名账单，见 `_to_items`
        by_model_group=_to_items(by_model_group, alias=alias, merge_aliased=True),
        by_model=_to_items(by_model),
        by_scene=_to_items(by_scene),
        by_status=_to_items(by_status),
    )


@router.get("/usage/trend", summary="用量与费用时间序列")
@require_role(Role.Admin)
async def get_usage_trend(
    instance_key: str = Query(default="", description=INSTANCE_DESC),
    tenant_id: Optional[int] = Query(default=None, description=TENANT_DESC),
    start_time: Optional[str] = Query(default=None, description=START_DESC),
    end_time: Optional[str] = Query(default=None, description=END_DESC),
    bucket_hours: int = Query(default=24, ge=1, le=24 * 30, description="分桶宽度（小时）"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> UsageTrendResponse:
    scope = await resolve_usage_scope(
        _current_user,
        instance_key=instance_key,
        tenant_id=tenant_id,
        start_time=start_time,
        end_time=end_time,
    )
    points = await aggregate_trend(scope, bucket_hours=bucket_hours)
    return UsageTrendResponse(
        start_time=scope.start_time.isoformat(),
        end_time=scope.end_time.isoformat(),
        bucket_hours=bucket_hours,
        points=[
            UsageTrendPoint(bucket_start=str(point["bucket_start"]), **_to_metrics(point).model_dump())  # type: ignore[arg-type]
            for point in points
        ],
    )


@router.get("/usage/records", summary="用量流水明细")
@require_role(Role.Admin)
async def list_usage_records(
    instance_key: str = Query(default="", description=INSTANCE_DESC),
    tenant_id: Optional[int] = Query(default=None, description=TENANT_DESC),
    start_time: Optional[str] = Query(default=None, description=START_DESC),
    end_time: Optional[str] = Query(default=None, description=END_DESC),
    scene: str = Query(default="", description="按场景过滤；留空表示全部"),
    status: str = Query(default="", description="按收尾形态过滤；留空表示全部"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _current_user: DBUser = Depends(get_current_active_user),
) -> UsageRecordListResponse:
    """逐条明细。出账要对得上号，聚合数字之外必须有可核对的原始行"""
    scope = await resolve_usage_scope(
        _current_user,
        instance_key=instance_key,
        tenant_id=tenant_id,
        start_time=start_time,
        end_time=end_time,
    )
    query = scope.records()
    if scene:
        query = query.filter(scene=scene)
    if status:
        query = query.filter(status=status)

    total = await query.count()
    rows = await query.order_by("-id").offset((page - 1) * page_size).limit(page_size)
    alias = await visible_model_group_names(_current_user, [row.model_group_name for row in rows])
    return UsageRecordListResponse(
        total=total,
        items=[
            UsageRecordItem(
                id=row.id,
                create_time=row.create_time.isoformat() if row.create_time else "",
                adapter_key=row.adapter_key,
                instance_key=row.instance_key,
                domain_id=row.domain_id,
                tenant_id=row.tenant_id,
                chat_key=row.chat_key,
                person_uid=row.person_uid,
                preset_id=row.preset_id,
                model_group_name=alias.get(row.model_group_name, row.model_group_name),
                model_name=row.model_name,
                stream_mode=row.stream_mode,
                token_input=row.token_input,
                token_output=row.token_output,
                token_total=row.token_total,
                cost=row.cost,
                scene=row.scene,
                status=row.status,
                error_type=row.error_type,
                finish_reason=row.finish_reason,
                attempt_index=row.attempt_index,
                attempt_total=row.attempt_total,
                is_fallback=row.is_fallback,
                generation_time_ms=row.generation_time_ms,
            )
            for row in rows
        ],
    )
