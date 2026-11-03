"""好感度的读写与热路径缓存

## 隔离键是 `(域, 人, 人设)`，由 :class:`.key.AffectionKey` 承载

本模块**从不自己拼键**：写侧的键只能来自 `AffectionKey.of_scope(scope)`，
读侧从一行已有记录读回它自己的键只能走 :func:`key_of_record`。理由见 `key.py` 的模块文档。

## 一个人可以有好几个账号

`platform_userid` 这一列存的是**人**，不是"发消息的那个账号"。于是本模块有两个地方
必须按"这个人的全部账号"取并集，而不是只看主账号那一个值：

- :func:`list_events` —— 后台看到的时间线，合并语义要求"流水取并集，保留来源账号"；
- :func:`window_usage` —— 刷分额度。两者若不一致，后台显示的流水与额度算的流水
  就是两本账，"为什么他今天涨不动了"永远解释不清。

两者共用 :func:`person_account_ids`。**没合并过的部署它只是一次空的索引点查**
（`identity_alias` 是空表），与今天的成本差别可以忽略。

## 为什么必须有缓存

好感度在**每一条消息**的提示词构造里都要读。没有缓存的话，一个几十人的活跃群每轮对话
都会多出若干次按隔离键的点查；这条链路又在主对话的关键路径上，任何一次慢查询都直接变成
"Bot 回话变慢"。缓存按隔离键存快照，带 TTL，写入时就地失效。

缓存**必须以完整的隔离键为键**：只用 `platform_userid` 做键会让实例 A 的好感度被实例 B
读到，这是本模块最容易写错、也最难在生产上察觉的一处。键换成五元组之后这条同样成立，
而且多了一个好处——改域、换人设、合并账号之后**旧快照因为键不同而自然失效**，
不需要任何显式的失效通知（`services/identity_scope.py` 的缓存取舍依赖这一点）。

## 空记录也要缓存

新人、路人、只在群里潜水的人占绝大多数，他们查出来都是"没有记录"。不缓存这个否定结果，
缓存对最常见的情况就完全不起作用了。

## 速率约束的用量从流水表算，不从内存计数器算

"最近一天涨了多少"直接对 `affection_event` 做聚合（:func:`window_usage`）。用进程内计数器
会有两个致命缺陷：重启即清零，等于给刷分的人送一次免费重置；多进程部署下每个进程各记一份，
额度会被悄悄放大成进程数倍。流水表本来就逐条记着 `delta` 与 `create_time`，它就是权威账本。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q
from tortoise.functions import Sum
from tortoise.queryset import QuerySet
from tortoise.timezone import now as db_now
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.services.identity_scope import ScopeDomain, resolve_affection_domain
from nekro_agent.tools.untrusted_content import sanitize_block_text, sanitize_inline_text

from .key import AffectionKey
from .stage import (
    AFFECTION_DAILY_WINDOW_SECONDS,
    AFFECTION_WEEKLY_WINDOW_SECONDS,
    AffectionEventSource,
    AffectionRateLimits,
    AffectionStage,
    AffectionWindowUsage,
    clamp_score,
    limit_delta,
    stage_of,
)

logger = get_sub_logger("affection")

# 落库前的长度上限。状态描述由 LLM 产出且会回注提示词，必须封顶，
# 否则一段超长描述能把整个上下文窗口挤爆。
MAX_NICKNAME_LENGTH = 64
MAX_STATE_TEXT_LENGTH = 300
MAX_EVENT_TEXT_LENGTH = 160

# 缓存条目上限。超出后整体清空而不是做精细淘汰：好感度快照重建代价极低，
# 与其为它维护一套 LRU，不如让缓存偶尔冷启动一次。
_CACHE_MAX_ENTRIES = 4096


@dataclass(frozen=True)
class AffectionSnapshot:
    """好感度的只读快照

    `exists` 区分"有记录但分数是 0"和"从来没打过交道"：前者要注入提示词，
    后者不该在提示词里凭空多出一个"陌生人"条目。
    """

    key: AffectionKey
    exists: bool
    score: int
    stage: AffectionStage
    nickname: str
    state_text: str
    last_event_text: str
    interaction_count: int

    @property
    def has_relationship(self) -> bool:
        """这一份快照里有没有**任何**关系可言

        `exists` 只说明"库里有这一行"，而行的存在不等于打过交道：账号合并的补偿会为一个
        从没在这个人设下说过话的人把行建出来（`_ensure_record`），撤销之后那一行会留下
        （物理删行与"不搬行、可撤销"这条原则冲突，见 `merge.py`），于是变成一行
        分数 0、没互动过、什么描述都没有的**空壳**。

        提示词按 `exists` 过滤时，这种空壳会渲染成一个"陌生人"条目——而
        `AffectionSnapshot.empty` 存在的全部理由就是"不该在提示词里凭空多出一个陌生人条目"。
        判据因此看**实质**而不是看行在不在：分数为 0、一次互动都没有、两段描述都空，
        这一行就没有携带任何信息，读侧当作没有。

        管理员重置过的人不会被误判：重置清空描述但 `interaction_count` 留着，
        那是真打过交道的证据。人工调到非 0 分的人同样保留。
        """
        return self.exists and not (
            self.score == 0 and self.interaction_count == 0 and not self.state_text and not self.last_event_text
        )

    @classmethod
    def empty(cls, key: AffectionKey) -> "AffectionSnapshot":
        return cls(
            key=key,
            exists=False,
            score=0,
            stage=stage_of(0),
            nickname="",
            state_text="",
            last_event_text="",
            interaction_count=0,
        )

    @classmethod
    def from_record(cls, record: DBAffection) -> "AffectionSnapshot":
        return cls(
            key=key_of_record(record),
            exists=True,
            score=record.score,
            stage=stage_of(record.score),
            nickname=record.nickname,
            state_text=record.state_text,
            last_event_text=record.last_event_text,
            interaction_count=record.interaction_count,
        )


_cache: Dict[AffectionKey, Tuple[float, AffectionSnapshot]] = {}


def key_of_record(record: DBAffection) -> AffectionKey:
    """从一行已经落库的记录读回它自己的隔离键

    后台的详情、人工调整、重置都是"对着一行已经存在的数据操作"——它们手上没有频道，
    因此没法走解析器。**这不是"自己拼键"**：五个字段逐列取自这一行自己，
    写回去必然落在同一行上。反过来，拿其中几列去猜另一个域的键就是串号。
    """
    return AffectionKey(
        adapter_key=record.adapter_key,
        instance_key=record.instance_key,
        domain_group_id=record.domain_group_id,
        person_uid=record.platform_userid,
        preset_id=record.preset_id,
    )


async def person_account_ids(key: AffectionKey) -> List[str]:
    """这个人在本域内的全部平台账号：主账号在前，别名按建立顺序在后

    合并语义要求"流水取并集"，而流水行上的 `platform_userid` 记的是**当时**解析出来的人；
    合并之前副账号自己就是一个人，它那些行的值是副账号本身。因此并集只能靠别名表现查，
    不能靠某一列的值推断。

    **删掉别名即撤销**：本函数下一次调用就不会再返回那个账号，两边的流水各自回到自己名下，
    一行数据都不用搬。这正是"不物理合并数据行"换来的性质。

    没合并过的人在 `identity_alias` 上是一次命中不了任何行的索引点查（零配置时该表为空），
    因此这条并集不是新增的成本大头。
    """
    accounts = [key.person_uid]
    rows = (
        await DBIdentityAlias.filter(domain_id=key.domain_id, person_uid=key.person_uid)
        .order_by("id")
        .values_list("platform_userid", flat=True)
    )
    for row in rows:
        account = str(row)
        if account and account not in accounts:
            accounts.append(account)
    return accounts


async def affection_domain_of_instance(instance_key: str, adapter_key: str = "") -> Optional[ScopeDomain]:
    """按实例标识解析好感域，供只拿得到 `instance_key` 的调用方（后台作用域）使用

    Args:
        instance_key: 实例标识；空串表示"没有作用域"，本函数不接受
        adapter_key: 调用方知道实例属于哪个适配器时必须传。不传时在同名的几行里取最早那行，
            而另一个适配器下的同名实例可以归别的租户——换算出来的就是别人的域。

    Returns:
        该实例的好感域；实例行查不到时返回 `None`。

    **实例行查不到时不是错误**：历史残留的未认领实例、以及被彻底删除后仍留有好感行的实例
    都会走到这里。这种实例必然是"自成一域"——域绑定行只可能由
    `DBInstanceDomainBinding.bind_to_groups` 写入，而它要求实例存在
    （`assert_instance_joinable` fail-closed），实例被 purge 时绑定行又会被
    `release_instances` 一并清掉。调用方因此可以安全地退化成"按实例列过滤"，
    也就是与今天逐字相同的行为。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    if not instance_key:
        return None
    # 取最早那行而不是 `get_or_none`：`instance_key` 的唯一性只在创建侧保证
    # （`console/instance_manager.py` 的全局重名校验），库里的唯一键是
    # `(adapter_key, instance_key)`，允许重名。与 `DBAdapterInstance.get_by_bound_account`
    # 同一取舍——脏行下确定性地取一行，而不是让后台整页 500。
    query = DBAdapterInstance.filter(instance_key=instance_key)
    if adapter_key:
        query = query.filter(adapter_key=adapter_key)
    instance = await query.order_by("id").first()
    if instance is None:
        return None
    return await resolve_affection_domain(instance.adapter_key, instance_key)


def invalidate_cache(key: Optional[AffectionKey] = None) -> None:
    """使缓存失效；不传键时清空全部"""
    if key is None:
        _cache.clear()
        return
    _cache.pop(key, None)


async def load_snapshots(keys: Sequence[AffectionKey], ttl_seconds: int) -> Dict[AffectionKey, AffectionSnapshot]:
    """批量读取好感度快照

    一次对话要看好几个人，逐个点查会把缓存未命中的代价乘上人数，因此未命中的部分
    合并成一次 `OR` 查询。

    Args:
        keys: 需要读取的隔离键，重复项会被去重
        ttl_seconds: 缓存有效期；非正值表示不使用缓存

    Returns:
        每个入参键都有一项，从未打过交道的返回 `AffectionSnapshot.empty()`
    """
    unique_keys: List[AffectionKey] = []
    for key in keys:
        if key not in unique_keys:
            unique_keys.append(key)

    now = time.time()
    result: Dict[AffectionKey, AffectionSnapshot] = {}
    missing: List[AffectionKey] = []
    for key in unique_keys:
        cached = _cache.get(key) if ttl_seconds > 0 else None
        if cached is not None and cached[0] > now:
            result[key] = cached[1]
        else:
            missing.append(key)

    if missing:
        for key, snapshot in (await _query_snapshots(missing)).items():
            result[key] = snapshot
            _remember(key, snapshot, ttl_seconds, now)

    return result


async def load_snapshot(key: AffectionKey, ttl_seconds: int) -> AffectionSnapshot:
    """读取单个好感度快照"""
    return (await load_snapshots([key], ttl_seconds))[key]


async def apply_delta(
    key: AffectionKey,
    *,
    delta: int,
    reason: str,
    source: AffectionEventSource,
    chat_key: str = "",
    nickname: str = "",
    state_text: Optional[str] = None,
    event_text: Optional[str] = None,
    operator: str = "",
    bump_interaction: bool = True,
    source_platform_userid: str = "",
    update_event_text: bool = True,
) -> AffectionSnapshot:
    """按增量更新好感度，并记录一条变化事件

    Args:
        key: 隔离键
        delta: 变化量，调用方须先经 `clamp_delta` 收敛
        reason: 变化原因叙述（不可信内容，本函数负责清洗）
        source: 变化来源
        chat_key: 触发本次变化的会话标识，仅用于展示
        nickname: 最近一次见到的昵称（不可信内容）
        state_text: 新的状态描述；传 None 表示保留原值
        event_text: 新的变化叙述；传 None 时回落到 `reason`
        operator: 人工调整的操作者用户名
        bump_interaction: 是否累加互动轮次；人工调整应传 False
        source_platform_userid: 触发本次变化的**原始账号**。与 `key.person_uid` 相同时留空串
            （这也是没合并过的人与全部存量行的取值）。合并之后一个人有好几个账号，
            不记这一列就再也说不清"这条是哪个号干的"
        update_event_text: 是否刷新"最近一次变化叙述"。账号合并的补偿事件传 False——
            那不是这段关系里发生的事，把它写进 `last_event_text` 会被下一轮提示词
            当成"最近发生了什么"念给用户听

    Returns:
        更新后的快照
    """
    return await _write(
        key,
        delta=delta,
        target_score=None,
        reason=reason,
        source=source,
        chat_key=chat_key,
        nickname=nickname,
        state_text=state_text,
        event_text=event_text,
        operator=operator,
        bump_interaction=bump_interaction,
        source_platform_userid=source_platform_userid,
        update_event_text=update_event_text,
    )


async def set_score(
    key: AffectionKey,
    *,
    score: int,
    reason: str,
    source: AffectionEventSource,
    operator: str = "",
    clear_state_text: bool = False,
) -> AffectionSnapshot:
    """把好感度直接设为指定值（后台调整/重置用）

    目标值在锁内换算成增量，因此与自动评估共享同一份并发保护：
    先读一遍再算差额会让"管理员点保存"与"后台评估刚好写入"互相覆盖。
    """
    return await _write(
        key,
        delta=0,
        target_score=clamp_score(score),
        reason=reason,
        source=source,
        state_text="" if clear_state_text else None,
        event_text="" if clear_state_text else None,
        operator=operator,
        bump_interaction=False,
    )


async def _write(
    key: AffectionKey,
    *,
    delta: int,
    target_score: Optional[int],
    reason: str,
    source: AffectionEventSource,
    chat_key: str = "",
    nickname: str = "",
    state_text: Optional[str] = None,
    event_text: Optional[str] = None,
    operator: str = "",
    bump_interaction: bool = True,
    source_platform_userid: str = "",
    update_event_text: bool = True,
) -> AffectionSnapshot:
    """好感度的唯一写入口

    读改写放在同一个事务的行锁下：同一个人可能同时在多个群里说话，
    两条并发的评估各读到旧分数再各自写回，其中一次的变化就凭空消失了。
    行锁只能锁住已存在的行，因此建行动作提前到事务之外先做掉。

    **过滤与建行都用 `key.as_columns()` 这同一份列**：写进去的行自己查不到是这套五列键上
    最容易犯、也最难查的错（表现为每次写入都新建一行，分数永远停在第一次的增量上）。

    时间戳一律取 `tortoise.timezone.now()`：同表的 `create_time` 由 `auto_now_add` 写成
    带时区的值，这两列若写成朴素本地时间，同一列里就会混着两种表示，把"这条状态是什么时候
    生成的"变成一个要看是谁写的才能回答的问题。
    """
    clean_reason = sanitize_inline_text(reason, MAX_EVENT_TEXT_LENGTH)
    clean_nickname = sanitize_inline_text(nickname, MAX_NICKNAME_LENGTH)
    clean_state = sanitize_block_text(state_text, MAX_STATE_TEXT_LENGTH) if state_text is not None else None
    clean_event = sanitize_inline_text(event_text, MAX_EVENT_TEXT_LENGTH) if event_text is not None else clean_reason

    await _ensure_record(key)

    async with in_transaction():
        record = await DBAffection.filter(**key.as_columns()).select_for_update().first()
        if record is None:  # pragma: no cover - `_ensure_record` 之后只有并发删除才会走到
            raise RuntimeError(f"好感度记录在写入前被删除: {key}")

        effective_delta = target_score - record.score if target_score is not None else delta
        record.score = clamp_score(record.score + effective_delta)
        record.stage = stage_of(record.score).value
        if clean_nickname:
            record.nickname = clean_nickname
        if clean_state is not None:
            record.state_text = clean_state
            record.state_generated_at = db_now()
        if update_event_text:
            record.last_event_text = clean_event
        if chat_key:
            record.last_chat_key = chat_key[:256]
        if bump_interaction:
            record.interaction_count += 1
            record.last_interact_at = db_now()
        await record.save()

        await DBAffectionEvent.create(
            **key.as_columns(),
            # 空串表示"与 platform_userid 相同"，见 `models/db_affection.py`。
            # 写成人自己的号码也不算错，但空串让"这条是副号干的"一眼可见。
            source_platform_userid="" if source_platform_userid == key.person_uid else source_platform_userid[:256],
            chat_key=chat_key[:256],
            delta=effective_delta,
            score_after=record.score,
            source=source.value,
            reason=clean_reason,
            operator=operator[:128],
        )

    snapshot = AffectionSnapshot.from_record(record)
    invalidate_cache(key)
    return snapshot


async def list_events(key: AffectionKey, limit: int = 20) -> List[DBAffectionEvent]:
    """读取某个人最近的好感度变化事件（**这个人全部账号的并集**，按时间从新到旧）

    合并过的账号在流水里仍然是各自的 `platform_userid`（不物理搬行），因此这里必须按
    :func:`person_account_ids` 取并集。撤销合并后并集自然缩回去，两边的历史各自完好。

    排序用 `create_time` 而不是 `id`：后台呈现的是**时间线**，而 id 序只是"谁先被写进这张表"。
    单进程下两者一致，但这条流水会被多进程/多节点同时写（好感度评估是后台任务），
    也会被历史数据导入写入带旧时间戳的行——那时按 id 排会把一条更早发生的事显示在最上面。
    同一时刻的并列再按 id 兜底，保证同一份数据两次读出来的顺序相同。
    """
    return await (
        DBAffectionEvent.filter(**key.scope_columns(), platform_userid__in=await person_account_ids(key))
        .order_by("-create_time", "-id")
        .limit(limit)
    )


async def throttle_llm_delta(
    key: AffectionKey,
    *,
    delta: int,
    current_score: int,
    limits: AffectionRateLimits,
    now: Optional[datetime] = None,
) -> int:
    """自动评估专用：把变化量收进递减收益与滚动窗口预算

    人工调整（`source=manual`）与重置（`source=reset`）**不经过这里**：它们直接调
    `apply_delta` / `set_score`，管理员来修数据时不该被"这个人今天已经涨够了"挡住。

    Args:
        key: 隔离键
        delta: 已经过 `clamp_delta` 收敛的变化量
        current_score: 本次变化之前的分数
        limits: 速率约束配置
        now: 窗口的终点，仅供测试注入；默认取当前时刻（须与 `window_usage` 的时钟一致）

    Returns:
        实际可以落库的变化量
    """
    if delta == 0:
        return 0
    unlimited = limits.daily_gain <= 0 and limits.daily_drop <= 0 and limits.weekly_gain <= 0
    usage = AffectionWindowUsage.unused() if unlimited else await window_usage(key, now=now)
    return limit_delta(delta, current_score=current_score, usage=usage, limits=limits)


async def window_usage(key: AffectionKey, *, now: Optional[datetime] = None) -> AffectionWindowUsage:
    """统计滚动窗口内由自动评估造成的累计涨跌

    只数 `source=llm` 的事件：人工调整、重置与账号合并的补偿都是运营手段，既不占用用户的额度，
    也不会因为管理员改了一次分就把额度"退"回来。

    额度按**人**算而不是按账号算：与 :func:`list_events` 用同一份账号并集。两者若不一致，
    后台显示的流水与额度算的流水就是两本账，而"换个号接着刷"恰好是这条额度要挡的事。

    算不出来时按"没用过额度"处理（fail-open）。这条链路在写路径上，统计查询失败不该让
    这一轮好感度整个丢掉；即便放行，单轮变化量仍有 `clamp_delta` 的硬上限兜底，
    最坏结果也就是多放行一轮的量。

    窗口终点取 `tortoise.timezone.now()` 而不是 `datetime.now()`：`create_time` 由
    `auto_now_add` 按 ORM 配置的时区写成**带时区**的值，拿一个朴素本地时间去比会整体
    错开一个时区偏移量——窗口不是变宽就是变窄，而且在本机与服务器上错得还不一样。
    """
    moment = now or db_now()
    daily_since = moment - timedelta(seconds=AFFECTION_DAILY_WINDOW_SECONDS)
    weekly_since = moment - timedelta(seconds=AFFECTION_WEEKLY_WINDOW_SECONDS)
    try:
        events = DBAffectionEvent.filter(
            **key.scope_columns(),
            platform_userid__in=await person_account_ids(key),
            source=AffectionEventSource.LLM.value,
        )
        return AffectionWindowUsage(
            daily_gain=await _sum_deltas(events, since=daily_since, positive=True),
            daily_drop=await _sum_deltas(events, since=daily_since, positive=False),
            weekly_gain=await _sum_deltas(events, since=weekly_since, positive=True),
        )
    except Exception as e:
        logger.warning(f"好感度窗口用量统计失败（本轮不做窗口限制）: key={key}, error={e}")
        return AffectionWindowUsage.unused()


async def _sum_deltas(events: QuerySet[DBAffectionEvent], *, since: datetime, positive: bool) -> int:
    """窗口内正/负变化量的绝对值之和

    在数据库里聚合而不是把行捞回来相加：一个活跃用户同时在几十个群里说话，
    七天的事件行数可以到几万条，而这里想要的只是一个数字。
    """
    scoped = events.filter(create_time__gte=since)
    scoped = scoped.filter(delta__gt=0) if positive else scoped.filter(delta__lt=0)
    rows = await scoped.annotate(total=Sum("delta")).values("total")
    total = rows[0]["total"] if rows else None
    return abs(int(total)) if total else 0


async def _query_snapshots(keys: Iterable[AffectionKey]) -> Dict[AffectionKey, AffectionSnapshot]:
    key_list = list(keys)
    condition = Q()
    for key in key_list:
        condition |= Q(**key.as_columns())

    records = await DBAffection.filter(condition)
    found = {key_of_record(record): AffectionSnapshot.from_record(record) for record in records}
    return {key: found.get(key, AffectionSnapshot.empty(key)) for key in key_list}


async def _ensure_record(key: AffectionKey) -> None:
    """保证该隔离键的行已存在

    行锁锁不住还不存在的行，所以两条并发的首次写入都会走到 INSERT。
    这里先把行建出来，让唯一约束去裁决竞争：输的一方拿到 IntegrityError，
    而它想要的结果（行已存在）恰好已经达成，直接继续即可。
    """
    exists = await DBAffection.filter(**key.as_columns()).exists()
    if exists:
        return
    try:
        await DBAffection.create(**key.as_columns())
    except IntegrityError:
        logger.debug(f"好感度记录已被并发创建，沿用既有行: {key}")


def _remember(key: AffectionKey, snapshot: AffectionSnapshot, ttl_seconds: int, now: float) -> None:
    if ttl_seconds <= 0:
        return
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        logger.debug(f"好感度缓存达到上限 {_CACHE_MAX_ENTRIES}，整体清空重建")
        _cache.clear()
    _cache[key] = (now + ttl_seconds, snapshot)
