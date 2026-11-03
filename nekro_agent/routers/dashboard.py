"""仪表盘统计路由

## 为什么这一族是"按实例过滤"而不是"提到平台方专属"

`/dashboard` 是前端的默认落地页（`Navigate to="/dashboard"`，路由本身没有角色守卫），
把它提到 `Role.Super` 会直接打掉租户与只读成员的首页。所以这里的做法是把
**七处聚合查询全部收进当前身份的数据范围**：平台方仍然看全站，租户只看自己名下实例
产生的消息与沙盒执行。

范围解析走 `DashboardScope`，它是本模块唯一的数据入口 —— 端点里不再出现裸的
`DBChatMessage.filter(...)` / `DBExecCode.filter(...)`，新加一个统计项时想绕过作用域
就得先把这条约定破坏掉。

两张表的分区依据不同：`chat_message` 自带 `adapter_key` / `instance_key` 两列，按列成对匹配
（只按 `instance_key` 会把另一个适配器下归别人的同名实例一起算进来）；
`exec_code` 只有 `chat_key`，走 `chat_channel` 的子查询把 `chat_key` 收敛到本身份的实例上。

**不要改回 `{adapter_key}-{instance_key}-` 前缀匹配**：那个格式只是 `BaseAdapter.build_chat_key`
的默认实现，`wechat_ilink_multi`（`wxim-{instance_key}:group:{id}`）与 `qqbot_openclaw`
（`qqoc-{channel_id}`，压根没有实例段）都不遵守它 —— 见
`adapters/__init__.py:CHAT_KEY_PREFIX_ALIASES` 的登记。前缀匹配对这些适配器恒不命中，
表现为租户在自己的首页上看到「有消息、零沙盒执行」这种自相矛盾的数字。
频道归属的口径只有一处：`chat_key -> chat_channel.(adapter_key, instance_key)`，
与 `routers/chat_channel.py:assert_channel_accessible` 逐字相同。

未启用多租户时 `get_tenant_scope()` 恒为 None，`DashboardScope.instances` 为 None，
每个查询都不加任何条件，行为与改造前逐字一致。
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from tortoise.expressions import Subquery
from tortoise.functions import Avg, Count
from tortoise.queryset import QuerySet
from tortoise.timezone import now as db_now

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.runtime_state import is_shutting_down
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.tenant_perm import (
    InstanceRef,
    accessible_instance_refs,
    get_tenant_scope,
    instance_refs_filter,
)

logger = get_sub_logger("dashboard")

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# 并发限制：避免同时发出过多数据库查询占满连接池
_SEMAPHORE = asyncio.Semaphore(8)

# 历史桶缓存：key = (作用域标签, bucket_start_iso, bucket_end_iso)，value = 该桶的统计结果。
# 只缓存已完全过去的桶（t_end <= 查询时刻），这类桶的数据不会再变化。
#
# **作用域标签必须在键里**：不带它的话，第一个租户查过的桶会被平台方与其他租户直接复用，
# 这个性能缓存本身就成了一条跨租户读取通道。
_bucket_cache: Dict[Tuple[str, str, str], Dict[str, Union[int, float]]] = {}
# 飞行中的查询 Future，避免多个请求同时 miss cache 时重复查询同一个桶
_bucket_inflight: Dict[Tuple[str, str, str], "asyncio.Future[Dict[str, Union[int, float]]]"] = {}
# 缓存条目上限：键里加了作用域标签之后条目数会随租户数成倍增长，
# 而键是绝对时间戳、永远不会命中第二次，所以必须有个封顶。超限时整体丢弃重来（纯性能缓存，丢了只是变慢）。
_BUCKET_CACHE_MAX_ENTRIES = 4096


class DashboardOverview(BaseModel):
    total_messages: int
    active_sessions: int
    unique_users: int
    total_sandbox_calls: int
    success_calls: int
    failed_calls: int
    success_rate: float


class DistributionItem(BaseModel):
    label: str
    value: int
    percentage: float


class DistributionsResponse(BaseModel):
    stop_type: List[DistributionItem]
    message_type: List[DistributionItem]


class RankingItem(BaseModel):
    id: str
    name: str
    value: int


@dataclass(frozen=True)
class DashboardScope:
    """当前身份能统计到的数据范围

    `instances is None` 表示平台方 / 未启用多租户，查询不加任何条件；
    否则是该身份名下的实例（`(adapter_key, instance_key)`），空元组表示"一个实例都没有"，统计结果全为 0。
    """

    instances: Optional[Tuple[InstanceRef, ...]]

    @property
    def cache_tag(self) -> str:
        """历史桶缓存键里的作用域标签，平台方用 `*` 与任何租户都不相等"""
        if self.instances is None:
            return "*"
        return ",".join(f"{adapter_key}/{instance_key}" for adapter_key, instance_key in self.instances)

    def messages(self) -> QuerySet[DBChatMessage]:
        if self.instances is None:
            return DBChatMessage.all()
        return DBChatMessage.filter(instance_refs_filter(self.instances))

    def exec_codes(self) -> QuerySet[DBExecCode]:
        """`exec_code` 只有 `chat_key`，归属经 `chat_channel` 一跳推出来

        写成子查询而不是把频道 `chat_key` 全捞回来拼 `IN (...)`：一个租户的频道数不可控，
        绑定参数会撞上 SQLite 的上限。可见实例为空时子查询退化成 `WHERE 1=0`，
        整条条件恒空，不会退化成不受限。
        """
        if self.instances is None:
            return DBExecCode.all()
        return DBExecCode.filter(
            chat_key__in=Subquery(
                DBChatChannel.filter(instance_refs_filter(self.instances)).values("chat_key"),
            ),
        )


async def resolve_dashboard_scope(user: DBUser) -> DashboardScope:
    """解析当前身份的统计范围

    平台方在第一步就返回，一次实例查询都不做。
    """
    if get_tenant_scope(user) is None:
        return DashboardScope(instances=None)
    return DashboardScope(instances=tuple(sorted(await accessible_instance_refs(user))))


async def get_time_range(time_range: str = "day") -> datetime:
    now = db_now()
    if time_range == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_range == "week":
        start_time = now - timedelta(days=now.weekday())
        return start_time.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_range == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=1)


async def _count_distinct(queryset: QuerySet, field: str) -> int:
    """在数据库侧执行 COUNT(DISTINCT field)，避免将列表传回应用层。"""
    result = await queryset.annotate(cnt=Count(field, distinct=True)).values("cnt")
    return int(result[0]["cnt"]) if result else 0


async def _fetch_stream_bucket(
    scope: DashboardScope,
    bucket_start: datetime,
    bucket_end: datetime,
) -> Dict[str, Union[int, float]]:
    """查询单个时间桶的实时统计数据。

    对已完全过去的桶使用内存缓存，避免重复查询不会再变化的历史数据。
    并发请求同一个桶时，后续请求等待首个查询的结果（不重复查 DB）。
    """
    cache_key = (scope.cache_tag, bucket_start.isoformat(), bucket_end.isoformat())
    now = db_now()
    is_historical = bucket_end <= now

    # 命中持久缓存：直接返回
    if is_historical and cache_key in _bucket_cache:
        logger.debug(f"bucket cache HIT {bucket_start.strftime('%H:%M')}~{bucket_end.strftime('%H:%M')}")
        return _bucket_cache[cache_key]

    # 命中飞行中的查询：等待其完成，共享结果
    if cache_key in _bucket_inflight:
        return await asyncio.shield(_bucket_inflight[cache_key])

    # 未命中：创建 Future 并注册，让并发请求可以等待
    loop = asyncio.get_event_loop()
    future: asyncio.Future[Dict[str, Union[int, float]]] = loop.create_future()
    if is_historical:
        _bucket_inflight[cache_key] = future

    _t0 = time.monotonic()
    try:
        messages, sandbox_calls, success_calls, avg_result = await asyncio.gather(
            scope.messages().filter(create_time__gte=bucket_start, create_time__lt=bucket_end).count(),
            scope.exec_codes().filter(create_time__gte=bucket_start, create_time__lt=bucket_end).count(),
            scope.exec_codes().filter(create_time__gte=bucket_start, create_time__lt=bucket_end, success=True).count(),
            scope.exec_codes()
            .filter(create_time__gte=bucket_start, create_time__lt=bucket_end)
            .annotate(avg=Avg("exec_time_ms"))
            .values("avg"),
        )

        avg_val = avg_result[0]["avg"] if avg_result else None
        avg_exec_time = float(avg_val) if avg_val is not None else 0.0

        result: Dict[str, Union[int, float]] = {
            "recent_messages": messages,
            "recent_sandbox_calls": sandbox_calls,
            "recent_success_calls": success_calls,
            "recent_avg_exec_time": round(avg_exec_time, 2),
        }

        _elapsed = time.monotonic() - _t0
        logger.debug(f"bucket cache MISS {bucket_start.strftime('%H:%M')}~{bucket_end.strftime('%H:%M')} => {_elapsed*1000:.1f}ms | msgs={messages} exec={sandbox_calls}")

        if is_historical:
            if len(_bucket_cache) >= _BUCKET_CACHE_MAX_ENTRIES:
                _bucket_cache.clear()
            _bucket_cache[cache_key] = result
            future.set_result(result)

        return result
    except Exception as exc:
        if is_historical and not future.done():
            future.set_exception(exc)
        raise
    finally:
        _bucket_inflight.pop(cache_key, None)


@router.get("/overview", summary="获取仪表盘概览数据")
async def get_dashboard_overview(
    time_range: str = "day",
    _current_user: DBUser = Depends(get_current_active_user),
) -> DashboardOverview:
    scope = await resolve_dashboard_scope(_current_user)
    start_time = await get_time_range(time_range)

    # 5 个查询全部并行：COUNT(DISTINCT) 在数据库侧完成，不传回列表
    (
        total_messages,
        active_sessions,
        unique_users,
        total_sandbox_calls,
        success_calls,
    ) = await asyncio.gather(
        scope.messages().filter(create_time__gte=start_time).count(),
        _count_distinct(scope.messages().filter(create_time__gte=start_time), "chat_key"),
        _count_distinct(scope.messages().filter(create_time__gte=start_time), "sender_id"),
        scope.exec_codes().filter(create_time__gte=start_time).count(),
        scope.exec_codes().filter(create_time__gte=start_time, success=True).count(),
    )

    failed_calls = total_sandbox_calls - success_calls
    success_rate = round(success_calls / total_sandbox_calls * 100, 2) if total_sandbox_calls > 0 else 0

    return DashboardOverview(
        total_messages=total_messages,
        active_sessions=active_sessions,
        unique_users=unique_users,
        total_sandbox_calls=total_sandbox_calls,
        success_calls=success_calls,
        failed_calls=failed_calls,
        success_rate=success_rate,
    )


async def _fetch_trends_interval(
    scope: DashboardScope,
    current_time: datetime,
    next_time: datetime,
    metrics_list: List[str],
) -> Dict[str, Union[str, int, float]]:
    """查询单个时间区间内的指标数据，区间内各查询并行执行。"""
    async with _SEMAPHORE:
        need_sandbox = any(m in metrics_list for m in ["sandbox_calls", "success_calls", "failed_calls", "success_rate"])
        need_success = any(m in metrics_list for m in ["success_calls", "failed_calls", "success_rate"])

        coroutines: List[object] = []
        keys: List[str] = []

        if "messages" in metrics_list:
            keys.append("messages")
            coroutines.append(scope.messages().filter(create_time__gte=current_time, create_time__lt=next_time).count())

        if need_sandbox:
            keys.append("sandbox_calls")
            coroutines.append(
                scope.exec_codes().filter(create_time__gte=current_time, create_time__lt=next_time).count()
            )

        if need_success:
            keys.append("success_calls")
            coroutines.append(
                scope.exec_codes()
                .filter(create_time__gte=current_time, create_time__lt=next_time, success=True)
                .count()
            )

        results = await asyncio.gather(*coroutines)  # type: ignore[arg-type]
        fetched: Dict[str, int] = dict(zip(keys, results))

        data_point: Dict[str, Union[str, int, float]] = {"timestamp": current_time.isoformat()}

        if "messages" in metrics_list:
            data_point["messages"] = fetched.get("messages", 0)

        if need_sandbox:
            sandbox_calls = fetched.get("sandbox_calls", 0)
            if "sandbox_calls" in metrics_list:
                data_point["sandbox_calls"] = sandbox_calls

            if need_success:
                success_calls = fetched.get("success_calls", 0)
                if "success_calls" in metrics_list:
                    data_point["success_calls"] = success_calls
                if "failed_calls" in metrics_list:
                    data_point["failed_calls"] = sandbox_calls - success_calls
                if "success_rate" in metrics_list:
                    data_point["success_rate"] = (
                        round(success_calls / sandbox_calls * 100, 2) if sandbox_calls > 0 else 0
                    )

        return data_point


@router.get("/trends", summary="获取趋势数据")
async def get_trends(
    metrics: str,
    time_range: str = "day",
    interval: str = "hour",
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[Dict[str, Union[str, int, float]]]:
    """获取趋势数据"""
    scope = await resolve_dashboard_scope(_current_user)
    now = db_now()
    if time_range == "day":
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        intervals = 24
        delta = timedelta(hours=1)
    elif time_range == "week":
        start_time = now - timedelta(days=now.weekday())
        start_time = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        if interval == "day":
            intervals = 7
            delta = timedelta(days=1)
        else:
            intervals = 7 * 24
            delta = timedelta(hours=1)
    elif time_range == "month":
        start_time = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        intervals = 30
        delta = timedelta(days=1)
    else:
        start_time = now - timedelta(days=1)
        intervals = 24
        delta = timedelta(hours=1)

    metrics_list = metrics.split(",")

    # 预先生成所有区间的时间边界
    time_slots: List[tuple[datetime, datetime]] = []
    current_time = start_time
    for _ in range(intervals):
        next_time = current_time + delta
        time_slots.append((current_time, next_time))
        current_time = next_time

    # 所有区间并行查询（内部已通过 Semaphore 限流）
    result = await asyncio.gather(
        *[_fetch_trends_interval(scope, t_start, t_end, metrics_list) for t_start, t_end in time_slots]
    )

    return list(result)


@router.get("/ranking", summary="获取排名数据")
async def get_ranking(
    ranking_type: str,
    time_range: str = "day",
    # 不加边界时 `limit=-1` 会让 Tortoise 抛 `ParamsError`，端点直接 500；
    # 前端从不传这个参数，收窄成 422 不改变任何既有调用。
    limit: int = Query(10, description="返回条数", ge=1, le=100),
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[RankingItem]:
    scope = await resolve_dashboard_scope(_current_user)
    start_time = await get_time_range(time_range)

    if ranking_type == "users":
        # 数据库端 GROUP BY + COUNT + ORDER BY + LIMIT，不再全量加载到内存
        rows = (
            await scope.exec_codes()
            .filter(
                create_time__gte=start_time,
                trigger_user_id__not_in=["0", "-1", ""],
            )
            .annotate(cnt=Count("id"))
            .group_by("trigger_user_id", "trigger_user_name")
            .order_by("-cnt")
            .limit(limit)
            .values("trigger_user_id", "trigger_user_name", "cnt")
        )
        return [
            RankingItem(
                id=str(r["trigger_user_id"]),
                name=str(r["trigger_user_name"]),
                value=int(r["cnt"]),
            )
            for r in rows
        ]

    return []


@router.get("/stats/stream", summary="获取实时统计数据流")
async def get_stats_stream(
    request: Request,
    granularity: int = Query(10, description="数据粒度（分钟）", ge=1, le=60),
    _current_user: DBUser = Depends(get_current_active_user),
):
    # 作用域必须在建流之前解析：`EventSourceResponse` 一旦返回状态码就已经是 200，
    # 生成器里再抛异常只表现为一条建立后立刻断开的连接。
    scope = await resolve_dashboard_scope(_current_user)

    async def generate():
        # 初始化基准计数（并行）
        last_message_count, last_sandbox_count, last_success_count = await asyncio.gather(
            scope.messages().count(),
            scope.exec_codes().count(),
            scope.exec_codes().filter(success=True).count(),
        )

        start_time = db_now() - timedelta(minutes=granularity * 50)
        current_time = start_time.replace(
            minute=(start_time.minute // granularity) * granularity,
            second=0,
            microsecond=0,
        )

        # 历史回放：逐桶串行查询并立即 yield
        # 命中缓存的桶直接返回（不走 DB），未缓存的桶查完即发，前端可实时看到图表填充
        while current_time < db_now():
            next_time = current_time + timedelta(minutes=granularity)
            bucket_data = await _fetch_stream_bucket(scope, current_time, next_time)
            yield json.dumps({"timestamp": current_time.isoformat(), **bucket_data})
            await asyncio.sleep(0)
            current_time = next_time

        now = db_now()
        next_aligned_time = now.replace(
            minute=((now.minute // granularity) * granularity + granularity) % 60,
            second=0,
            microsecond=0,
        )
        if next_aligned_time.minute < now.minute:
            next_aligned_time = next_aligned_time.replace(hour=next_aligned_time.hour + 1)

        # 实时阶段：作用域内差分计数 + avg 聚合均值，均并行执行
        while not is_shutting_down():
            if await request.is_disconnected():
                return
            wait_seconds = (next_aligned_time - db_now()).total_seconds()
            while wait_seconds > 0:
                if is_shutting_down() or await request.is_disconnected():
                    return
                sleep_seconds = min(wait_seconds, 1.0)
                await asyncio.sleep(sleep_seconds)
                wait_seconds -= sleep_seconds

            window_start = db_now() - timedelta(minutes=granularity)
            current_message_count, current_sandbox_count, current_success_count, avg_result = await asyncio.gather(
                scope.messages().count(),
                scope.exec_codes().count(),
                scope.exec_codes().filter(success=True).count(),
                scope.exec_codes()
                .filter(create_time__gte=window_start)
                .annotate(avg=Avg("exec_time_ms"))
                .values("avg"),
            )

            recent_messages = current_message_count - last_message_count
            recent_sandbox_calls = current_sandbox_count - last_sandbox_count
            recent_success_calls = current_success_count - last_success_count

            avg_val = avg_result[0]["avg"] if avg_result else None
            recent_avg_exec_time = float(avg_val) if avg_val is not None else 0.0

            last_message_count = current_message_count
            last_sandbox_count = current_sandbox_count
            last_success_count = current_success_count

            yield json.dumps(
                {
                    "timestamp": next_aligned_time.isoformat(),
                    "recent_messages": recent_messages,
                    "recent_sandbox_calls": recent_sandbox_calls,
                    "recent_success_calls": recent_success_calls,
                    "recent_avg_exec_time": round(recent_avg_exec_time, 2),
                },
            )

            next_aligned_time = next_aligned_time + timedelta(minutes=granularity)

    return EventSourceResponse(generate())


@router.get("/distributions", summary="获取所有分布数据")
async def get_distributions(
    time_range: str = "day",
    _current_user: DBUser = Depends(get_current_active_user),
) -> DistributionsResponse:
    scope = await resolve_dashboard_scope(_current_user)
    start_time = await get_time_range(time_range)

    stop_types = list(ExecStopType)

    # 所有 stop_type count + total_execs + 3 个 message_type count 全部并行
    total_execs_coro = scope.exec_codes().filter(create_time__gte=start_time).count()
    stop_type_coros = [
        scope.exec_codes().filter(create_time__gte=start_time, stop_type=st).count() for st in stop_types
    ]
    total_messages_coro = scope.messages().filter(create_time__gte=start_time).count()
    group_coro = scope.messages().filter(create_time__gte=start_time, chat_type=ChatType.GROUP).count()
    private_coro = scope.messages().filter(create_time__gte=start_time, chat_type=ChatType.PRIVATE).count()
    unknown_coro = scope.messages().filter(create_time__gte=start_time, chat_type=ChatType.UNKNOWN).count()

    all_results = await asyncio.gather(
        total_execs_coro,
        *stop_type_coros,
        total_messages_coro,
        group_coro,
        private_coro,
        unknown_coro,
    )

    total_execs: int = all_results[0]
    stop_type_counts: List[int] = list(all_results[1 : 1 + len(stop_types)])
    total_messages: int = all_results[1 + len(stop_types)]
    group_count: int = all_results[2 + len(stop_types)]
    private_count: int = all_results[3 + len(stop_types)]
    unknown_count: int = all_results[4 + len(stop_types)]

    stop_type_data: List[DistributionItem] = []
    if total_execs > 0:
        for stop_type, count in zip(stop_types, stop_type_counts):
            if count > 0:
                stop_type_data.append(
                    DistributionItem(
                        label=str(stop_type.value),
                        value=count,
                        percentage=round(count / total_execs * 100, 2),
                    ),
                )

    message_type_data: List[DistributionItem] = []
    if total_messages > 0:
        message_type_data = [
            DistributionItem(
                label="群聊消息",
                value=group_count,
                percentage=round(group_count / total_messages * 100, 2),
            ),
            DistributionItem(
                label="私聊消息",
                value=private_count,
                percentage=round(private_count / total_messages * 100, 2),
            ),
            DistributionItem(
                label="未知来源",
                value=unknown_count,
                percentage=round(unknown_count / total_messages * 100, 2),
            ),
        ]

    return DistributionsResponse(
        stop_type=stop_type_data,
        message_type=message_type_data,
    )
