"""用量流水的读侧：作用域 + 聚合

## 作用域只有一处判据

`UsageScope` 是本模块**唯一**的数据入口——路由里不出现裸的 `DBLLMUsageRecord.filter(...)`。
形态照抄 `routers/dashboard.py:DashboardScope`：新加一个统计项想绕过作用域，
就得先把这条约定破坏掉，而那是一次显式的改动，不是"忘了加一个 filter"。

判定复用现成设施，一条都不新写：

| 判什么 | 用谁 |
|---|---|
| 我是平台方还是某个租户 | `tenant_perm.get_tenant_scope` |
| 这个 `instance_key` 我够不够得着 | `tenant_perm.assert_instance_accessible`（越权 **404**） |
| 这个 `tenant_id` 我够不够得着 | `tenant_perm.assert_tenant_accessible`（越权 **404**） |
| 这个模型组名我看不看得见 | `model_group_service.can_view_group` |

## 为什么模型组名要按 `can_view_group` 掩码

`can_view_group` 的契约是「租户看不到的组**根本不出现**」——连 404/403 的差异都不给，
免得被用来反推平台方配了哪些组。用量页要是原样吐出 `model_group_name`，
这条不变式就从另一扇门被绕开了：租户的会话跑在平台方的组上，那个组名会直接出现在他的账单里。

**`model_name` 不掩码**，这是有意的取舍：模型是计费单位本身（同一个组换个模型，单价差一个
数量级），账单上不写清"这些 token 烧在哪个模型上"就等于给了一张没有明细的发票。
而模型 ID 是公开信息（`gpt-4o`、`gemini-2.5-flash`），它泄露不出"平台方内部怎么配的组"。

## 时间窗

默认最近 30 天，最长 366 天。上限不是性能考虑而是**语义**：这张表是流水，
不设窗就等于每次打开页面都全表扫描，而账单本来就是按周期看的。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

from tortoise.functions import Count, Sum
from tortoise.queryset import QuerySet
from tortoise.timezone import now as db_now

from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.model_group_service import can_view_group, resolve_console_identity
from nekro_agent.services.user.tenant_perm import (
    assert_instance_accessible,
    assert_tenant_accessible,
    get_tenant_scope,
    is_platform_owner,
)

# 租户看不到的那些模型组在账单上显示成这个。**不用 `CREDENTIAL_MASK`**：
# 那个哨兵在写入侧有"保持不变"的语义，混用会让某天有人把它当成可回填的值。
HIDDEN_MODEL_GROUP_LABEL = "(未公开的模型组)"

# 时间窗的默认与上限，见模块文档
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
# 趋势接口的分桶上限。桶数直接等于查询数（按天聚合的 SQL 写法在 sqlite 与 postgres 上
# 不通用，这里改成"每桶一次范围查询 + gather"，与 `dashboard._fetch_stream_bucket` 同形）
MAX_TREND_BUCKETS = 92

# 聚合列的别名 → 被聚合的列。**别名不能与模型字段同名**：Tortoise 的 `annotate` 会与
# 同名字段撞车。列在一处，免得四种分组各写一遍 `Sum(...)`（同一判据的第 N 处实现）。
_AGGREGATE_SOURCES = {
    "agg_token_input": "token_input",
    "agg_token_input_cached": "token_input_cached",
    "agg_token_output": "token_output",
    "agg_token_total": "token_total",
    "agg_cost": "cost",
}


def _aggregates() -> Dict[str, object]:
    """每次查询都新建一份聚合表达式

    **不要复用模块级的那几个 `Sum(...)` 对象**：Tortoise 的函数表达式在解析时会带上
    所属 queryset 的上下文，同一个实例串在多个查询上是未定义行为。
    """
    fields: Dict[str, object] = {"agg_records": Count("id")}
    for alias, source in _AGGREGATE_SOURCES.items():
        fields[alias] = Sum(source)
    return fields


@dataclass(frozen=True)
class UsageScope:
    """当前身份在用量流水上的数据范围

    Attributes:
        tenant_id: 租户过滤；`None` = 不按租户过滤（只有平台方且没传 `tenant_id` 时如此）
        instance_key: 实例过滤；空串 = 不按实例过滤
        adapter_key: 与 `instance_key` 配对的适配器；`instance_key` 为空时无意义
        start_time: 时间窗起点（含）
        end_time: 时间窗终点（不含）
        is_platform_owner: 调用者是不是平台方（决定模型组名要不要掩码）
    """

    tenant_id: Optional[int]
    instance_key: str
    adapter_key: str
    start_time: datetime
    end_time: datetime
    is_platform_owner: bool

    def records(self) -> QuerySet[DBLLMUsageRecord]:
        """本身份能看到的那些流水行

        租户身份下 `tenant_id` 恒非 None，因此 `tenant_id IS NULL` 的那些行
        （平台方自己的插件生成、连通性测试、以及归不了属的 `UNATTRIBUTED`）
        对租户天然不可见——不需要额外写一条排除条件。
        """
        query = DBLLMUsageRecord.filter(create_time__gte=self.start_time, create_time__lt=self.end_time)
        if self.tenant_id is not None:
            query = query.filter(tenant_id=self.tenant_id)
        if self.instance_key:
            query = query.filter(adapter_key=self.adapter_key, instance_key=self.instance_key)
        return query


def _parse_time(raw: Optional[str], field: str) -> Optional[datetime]:
    """解析 ISO 8601 时间；解析不动就 400 而不是悄悄回落到默认窗

    悄悄回落的代价是"我明明查了 3 月，页面却给我最近 30 天"，而且没有任何提示——
    对账页面上这种错法比报错贵得多。
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(reason=f"{field} 不是合法的 ISO 8601 时间: {raw}") from exc
    if parsed.tzinfo is None:
        # 无时区的入参按数据库时区解释，与库里 `create_time` 的口径一致
        parsed = parsed.replace(tzinfo=db_now().tzinfo)
    return parsed


def _default_end() -> datetime:
    """默认的窗口右端：`此刻` **向上取整到下一秒**

    窗口是半开的 `[start, end)`——按桶聚合要求相邻桶不重不漏，这一点不能改。
    但把默认右端定成裸的 `db_now()` 会漏掉"刚刚写进去的那几行"：Windows 上
    `datetime.now()` 的时钟粒度是 ~15.6ms，一次刚落库的流水与紧接着算出来的 `now`
    完全可能落在**同一个刻度**上，于是 `create_time < end` 不成立、那一行看不见。
    实测就是这样：同一条用例单跑必过、连跑时红——典型的时序 flaky。

    向上取整到下一秒是确定性的：任何在调用之前写入的行都严格早于它。
    对趋势分桶没有影响（那条路径的两端都由调用方显式给出）。
    """
    now = db_now()
    return now.replace(microsecond=0) + timedelta(seconds=1)


def resolve_time_window(start_time: Optional[str], end_time: Optional[str]) -> Tuple[datetime, datetime]:
    """把入参解析成 `[start, end)`，并施加默认窗与最长窗"""
    end = _parse_time(end_time, "end_time") or _default_end()
    start = _parse_time(start_time, "start_time") or (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    if start >= end:
        raise ValidationError(reason="start_time 必须早于 end_time")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise ValidationError(reason=f"查询时间窗最长 {MAX_WINDOW_DAYS} 天")
    return start, end


async def resolve_usage_scope(
    user: DBUser,
    *,
    instance_key: str = "",
    tenant_id: Optional[int] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> UsageScope:
    """解析当前身份 + 本次请求参数下的数据范围

    越权一律 **404**（不是 403）：403 会告诉租户"这个实例/租户确实存在"，
    等于把平台方的清单变成可枚举的。两条判定都直接调 `tenant_perm` 的现成守卫，
    本函数不自己比 `tenant_id`。
    """
    scope = get_tenant_scope(user)
    adapter_key = ""
    if instance_key:
        instance = await assert_instance_accessible(user, instance_key)
        adapter_key = instance.adapter_key
    if tenant_id is not None:
        # 平台方传了一个不存在的租户同样 404；租户传别人的租户号也走这一条
        await assert_tenant_accessible(user, tenant_id)

    start, end = resolve_time_window(start_time, end_time)
    return UsageScope(
        # 租户身份下**永远**以自己的作用域为准，请求里的 `tenant_id` 只可能等于它
        # （不等的话上面那句守卫已经 404 了），不存在"传空就看全部"这条路径。
        tenant_id=scope if scope is not None else tenant_id,
        instance_key=instance_key,
        adapter_key=adapter_key,
        start_time=start,
        end_time=end,
        is_platform_owner=is_platform_owner(user),
    )


async def aggregate_totals(scope: UsageScope) -> Dict[str, float]:
    """整个范围的合计"""
    aggregates = _aggregates()
    rows = await scope.records().annotate(**aggregates).values(*aggregates)
    return _normalize_row(rows[0] if rows else {})


async def aggregate_by(scope: UsageScope, field: str, *, limit: int = 50) -> List[Dict[str, object]]:
    """按某一列分组聚合，按费用从高到低取前 `limit` 组

    排序按**费用**而不是条数：一条超长上下文的请求可以顶一千条"好的"，
    按条数排出来的榜单会把最贵的那个实例排在第二页。
    """
    aggregates = _aggregates()
    rows = await (
        scope.records()
        .annotate(**aggregates)
        .group_by(field)
        .order_by("-agg_cost")
        .limit(limit)
        .values(field, *aggregates)
    )
    return [{"key": str(row.get(field) or ""), **_normalize_row(row)} for row in rows]


async def aggregate_trend(scope: UsageScope, *, bucket_hours: int = 24) -> List[Dict[str, object]]:
    """时间序列。每个桶一次范围查询，`asyncio.gather` 并发

    **不用 SQL 的日期截断函数**：`DATE_TRUNC`（postgres）与 `strftime`（sqlite）不通用，
    而本仓的测试跑在 sqlite 上、生产跑在 postgres 上——写一份只在其中一边成立的聚合，
    等于让这条路径的测试恒绿而线上恒错。
    """
    if bucket_hours <= 0:
        raise ValidationError(reason="bucket_hours 必须为正")
    span = scope.end_time - scope.start_time
    bucket = timedelta(hours=bucket_hours)
    bucket_count = max(1, -(-int(span.total_seconds()) // int(bucket.total_seconds())))
    if bucket_count > MAX_TREND_BUCKETS:
        raise ValidationError(reason=f"时间窗按 {bucket_hours} 小时分桶超过 {MAX_TREND_BUCKETS} 个，请缩短窗口或加大桶宽")

    starts = [scope.start_time + bucket * index for index in range(bucket_count)]

    async def one(bucket_start: datetime) -> Dict[str, object]:
        bucket_end = min(bucket_start + bucket, scope.end_time)
        aggregates = _aggregates()
        rows = await (
            scope.records()
            .filter(create_time__gte=bucket_start, create_time__lt=bucket_end)
            .annotate(**aggregates)
            .values(*aggregates)
        )
        return {"bucket_start": bucket_start.isoformat(), **_normalize_row(rows[0] if rows else {})}

    return list(await asyncio.gather(*(one(start) for start in starts)))


async def visible_model_group_names(user: DBUser, names: Sequence[str]) -> Dict[str, str]:
    """把出现在结果里的模型组名映射成"这个身份该看到的字符串"

    平台方拿到恒等映射（一次数据库查询都不做）。租户按 `can_view_group` 逐个判：
    看不见的组换成 :data:`HIDDEN_MODEL_GROUP_LABEL`。

    **库里已经没有那一行的组名也当作看不见**：组被删掉之后它到底属于谁已经无从判定，
    而"判不出来就放行"正是要堵的那个形态。
    """
    unique = [name for name in dict.fromkeys(names) if name]
    if not unique:
        return {}
    if is_platform_owner(user):
        return {name: name for name in unique}

    # 用 `model_group_service.resolve_console_identity` 而不是 `tenant_perm.resolve_current_identity`：
    # 后者是 async 且会为 `tenant_name` / `accessible_instance_keys` 额外查两次库，
    # 而 `can_view_group` 只用得到 `tenant_id` 与 `role`。判定本身仍然只有那一份。
    identity = resolve_console_identity(user)
    groups = await DBModelGroup.filter(name__in=unique)
    viewable: Set[str] = {group.name for group in groups if can_view_group(group, identity)}
    return {name: (name if name in viewable else HIDDEN_MODEL_GROUP_LABEL) for name in unique}


async def resolve_tenant_labels(user: DBUser, keys: Sequence[str]) -> Dict[str, str]:
    """把「按租户」那一档的分组键换成人读得懂的名字

    分组键是 `tenant_id` 的字符串形态（`aggregate_by` 统一 `str(...)`），空串 = `IS NULL`，
    也就是平台方自己的花销与归不了属的那些行。裸的 `1` / `7` 对出账没有任何意义 ——
    "每个客户烧了多少"正是这本账要回答的**第一个**问题。

    **不做可见性判定**：租户身份下 `records()` 已经把行按 `tenant_id` 过滤过了，
    这一档只可能出现他自己；平台方本来就看得到全部租户。也因此这里只查出现过的这一批 id，
    不整表拉——名字是给已经看得见的行配的标签，不是一份新的清单。

    **标签必须两两不同。** `DBTenant.display_name` **没有唯一约束**（唯一的是 `name`），
    两个客户完全可以都叫"测试"。撞名的后果不只是难看：分档表按 `key` 做 React 列表键，
    两行同名就是两行长得一模一样的账单——"哪个客户最贵"这个问题当场变成无解。
    所以撞了就退回唯一的 `name`（不是给所有人都加后缀：没撞的那些没必要变难读）。
    """
    del user  # 可见性由 `UsageScope.records()` 收敛，本函数不再判一次
    numeric = {key: int(key) for key in dict.fromkeys(keys) if key.isdigit()}
    if not numeric:
        return {}
    rows = await DBTenant.filter(id__in=sorted(set(numeric.values()))).values("id", "display_name", "name")
    slug = {int(row["id"]): str(row["name"] or "") for row in rows}
    label = {int(row["id"]): (str(row["display_name"] or "") or str(row["name"] or "")) for row in rows}
    duplicated = {value for value in label.values() if list(label.values()).count(value) > 1}
    # 查不到的租户（已删除）保留原始 id：账要对得上号，宁可显示一个号码也不能凭空消失
    return {
        key: (slug.get(tenant_id) or key) if label.get(tenant_id) in duplicated else (label.get(tenant_id) or key)
        for key, tenant_id in numeric.items()
    }


def _normalize_row(row: Dict[str, object]) -> Dict[str, float]:
    """把聚合结果里的 `None`（空分组）补成 0，并把计数收成 int

    `Sum` 在没有匹配行时回的是 `None`，直接透传出去会让前端在 `undefined` 上做算术、
    页面出现 `NaN`——这条路径在"某个实例今天一次都没跑"时必然走到。
    """
    return {
        "records": int(row.get("agg_records") or 0),
        "token_input": int(row.get("agg_token_input") or 0),
        "token_input_cached": int(row.get("agg_token_input_cached") or 0),
        "token_output": int(row.get("agg_token_output") or 0),
        "token_total": int(row.get("agg_token_total") or 0),
        "cost": float(row.get("agg_cost") or 0.0),
    }


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "HIDDEN_MODEL_GROUP_LABEL",
    "MAX_TREND_BUCKETS",
    "MAX_WINDOW_DAYS",
    "UsageScope",
    "aggregate_by",
    "aggregate_totals",
    "aggregate_trend",
    "resolve_tenant_labels",
    "resolve_time_window",
    "resolve_usage_scope",
    "visible_model_group_names",
]
