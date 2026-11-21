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

## 后台写入要留得住

后台的重置、只清印象、编辑印象都是「对着一行改描述」。它们本身当场生效，但评估是后台任务：
一种是下一轮评估照着同一段旧对话把内容复述回来，另一种是一轮在写入之前就开始的评估在写入
之后落库，把它盖回去。两件事共用一个判据——这个人这一行最近一次**后台写入事件**的
`create_time`（:func:`latest_admin_write_at`），不新增列：评估取对话的下界跟它取 `max`，
落库前再在行锁下复核一遍它变没变（:class:`AdminWriteGuard`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
from nekro_agent.tools.text_sanitize import strip_nul
from nekro_agent.tools.untrusted_content import sanitize_block_text, sanitize_inline_text

from .key import AffectionKey
from .stage import (
    ADMIN_WRITE_SOURCES,
    AFFECTION_DAILY_WINDOW_SECONDS,
    AFFECTION_WEEKLY_WINDOW_SECONDS,
    MODEL_SOURCES,
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
# 模型给一个人记的称呼。它会被下一轮提示词回注，同样要封顶；它比昵称短得多，
# 因为称呼是一句话里叫出来的东西，太长就不像称呼了。
MAX_CALL_NAME_LENGTH = 24

# 缓存条目上限。超出后整体清空而不是做精细淘汰：好感度快照重建代价极低，
# 与其为它维护一套 LRU，不如让缓存偶尔冷启动一次。
_CACHE_MAX_ENTRIES = 4096

# 「只动描述」事件的理由一律是宿主固定文案：不抄新旧描述——被清掉的那段文字可能正是
# 不该再出现的内容，抄进流水等于换个地方原样留着。
REASON_BLOCKED_CLEAR = "评估请求被内容审核拦下，去掉旧印象重试后成功；旧印象有嫌疑，已清空（分数不变）"
REASON_NARRATIVE_CLEAR = "管理员清空了印象（分数不变）"
REASON_NARRATIVE_EDIT = "管理员编辑了印象（分数不变）"
REASON_CALL_NAME = "改了称呼"


class AffectionWriteSuperseded(Exception):
    """落库前复核发现：这一行在评估开始之后有了新的后台写入，本次写入作废

    不是错误，是评估这一轮「输给了管理员」：调用方记一条日志、跳过这个人即可。
    """


@dataclass(frozen=True)
class AdminWriteGuard:
    """进行中评估的作废判据

    `seen` 是评估开始时这一行最近一次后台写入的时间（没有就是 None）。落库时在行锁下
    再取一次，不相等就说明这期间管理员动过这一行，评估的结果是照着旧状态算的，必须丢掉。
    比较的是**同一张表上读回来的同一个值**，不拿应用时钟去比，没有时钟偏差的问题。
    """

    seen: Optional[datetime]


@dataclass(frozen=True)
class AffectionWriteOutcome:
    """一次写入的「发生了什么」，只给模型写分的调用方用

    里程碑的判据要用三样东西：这次写入之前的阶段（决定「从」写什么）、写入之后的阶段
    （决定要不要写）、以及新建那条流水的 ID（里程碑段落要反指它）。这三样都只在
    :func:`_write` 的行锁里读得到：锁外再读一次，读到的是别人写入之后的状态，
    「从」就会写错，而且并发下两个调用方会各自以为自己是那条跃迁的见证者。

    `stage_before` 取自 `stage_of(record.score)` 在改分**之前**求值，`stage_after` 取自
    改分之后；`score_after` 是落库后的分数。三者都是行锁内的一致快照。
    """

    event_id: int
    stage_before: AffectionStage
    stage_after: AffectionStage
    score_after: int


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
    # 最近的关系里程碑引用，新的在前。存成元组而不是 list：快照是跨请求共享的缓存对象，
    # 一个可变列表被调用方就地 append 一下，就污染了整份缓存。
    milestone_refs: Tuple[Dict[str, Any], ...] = ()
    # 模型给这个人记的称呼；空串表示不特别称呼。也是不可信内容，读侧要清洗后再注入。
    call_name: str = ""
    # 最近一次**有好感记录的互动**：写分（后台评估或主模型自主调分）时更新，
    # 只说话不写分不算。久别提示按它算，不是按最近一次说话算。
    last_interact_at: Optional[datetime] = None

    @property
    def has_relationship(self) -> bool:
        """这一份快照里有没有**任何**关系可言

        `exists` 只说明"库里有这一行"，而行的存在不等于打过交道：账号合并的补偿会为一个
        从没在这个人设下说过话的人把行建出来（`_ensure_record`），撤销之后那一行会留下
        （物理删行与"不搬行、可撤销"这条原则冲突，见 `merge.py`），于是变成一行
        分数 0、没互动过、什么描述都没有的**空壳**。

        提示词按 `exists` 过滤时，这种空壳会渲染成一个"陌生人"条目——而
        `AffectionSnapshot.empty` 存在的全部理由就是"不该在提示词里凭空多出一个陌生人条目"。
        判据因此看**实质**而不是看行在不在：分数为 0、一次互动都没有、两段描述都空、
        也没记过称呼，这一行就没有携带任何信息，读侧当作没有。

        管理员重置过的人不会被误判：重置清空描述但 `interaction_count` 留着，
        那是真打过交道的证据。人工调到非 0 分的人同样保留。称呼也算实质：模型只给一个
        从没调过分的人记了称呼时，那一行只有 `call_name` 有内容，不算它就等于记了不显示。
        """
        return self.exists and not (
            self.score == 0
            and self.interaction_count == 0
            and not self.state_text
            and not self.last_event_text
            and not self.call_name
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
            milestone_refs=normalize_milestone_refs(record.milestone_refs),
            call_name=record.call_name,
            last_interact_at=record.last_interact_at,
        )


def normalize_milestone_refs(raw: object) -> Tuple[Dict[str, Any], ...]:
    """把库里的 `milestone_refs` 收敛成只含 dict 元素的元组

    脏值容错放在这里而不是每个读侧各写一遍：这一列是 JSON，历史数据、手工改库、
    将来的格式变更都可能让它变成别的东西。读侧一律拿到「可能是空的元组」，
    少一个 `isinstance` 就少一处会抛 `TypeError` 的提示词构造路径。
    """
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict))


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
    guard: Optional[AdminWriteGuard] = None,
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
        guard: 自动评估传入：落库前在行锁下复核这一行有没有新的后台写入，有就抛
            :class:`AffectionWriteSuperseded`、什么都不写

    Returns:
        更新后的快照
    """
    snapshot, _outcome = await _write(
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
        guard=guard,
    )
    return snapshot


async def apply_model_delta(
    key: AffectionKey,
    *,
    delta: int,
    reason: str,
    source: AffectionEventSource,
    chat_key: str = "",
    nickname: str = "",
    state_text: Optional[str] = None,
    event_text: Optional[str] = None,
    source_platform_userid: str = "",
    guard: Optional[AdminWriteGuard] = None,
) -> Tuple[AffectionSnapshot, AffectionWriteOutcome]:
    """模型写分的专用入口：除了快照，还返回这次写入「发生了什么」

    里程碑的判据要用到写入之前的阶段与新建流水的 ID，而这两样只在行锁里读得到
    （见 :class:`AffectionWriteOutcome`）。因此模型写分不能走 :func:`apply_delta`——
    那条路径只回快照，调用方拿到快照后再去查「刚才那一笔」，并发下会查到别人的。

    `source` 必须在 :data:`MODEL_SOURCES` 里：运营动作不该产出里程碑，把这条约束放在
    入口上，比让每个调用方自己记得别传 `manual` 更可靠。参数含义与 :func:`apply_delta` 相同。
    """
    if source not in MODEL_SOURCES:
        raise ValueError(f"只有模型写的分才走这里: {source}")
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
        bump_interaction=True,
        source_platform_userid=source_platform_userid,
        guard=guard,
    )


async def clear_narrative(
    key: AffectionKey,
    *,
    source: AffectionEventSource,
    reason: str,
    operator: str = "",
    chat_key: str = "",
    guard: Optional[AdminWriteGuard] = None,
    clear_call_name: bool = False,
) -> AffectionSnapshot:
    """只清两段描述（`state_text` / `last_event_text`），分数、阶段、互动次数都不动

    记一条 `delta=0` 的事件。后台「只清印象」与评估被拦后的清理共用这一个入口，
    区别只在 `source`、是否带 `guard` 与是否连称呼一起清。

    `clear_call_name` 只有后台「只清印象」传：管理员点了「清空」，称呼也是模型写的字，
    留着它就会出现「描述清空了，称呼还在叫」。评估被拦后的清理**不传**——称呼不在评估的
    提示词里，不是嫌疑内容，没有理由跟着一起丢。
    """
    snapshot, _outcome = await _write(
        key,
        delta=0,
        target_score=None,
        reason=reason,
        source=source,
        chat_key=chat_key,
        state_text="",
        event_text="",
        operator=operator,
        bump_interaction=False,
        guard=guard,
        call_name="" if clear_call_name else None,
    )
    return snapshot


async def edit_narrative(
    key: AffectionKey,
    *,
    state_text: str,
    last_event_text: str,
    operator: str,
    call_name: Optional[str] = None,
) -> AffectionSnapshot:
    """管理员改写两段描述，分数不动

    先去 NUL、再按两列各自的上限截断，然后照常走 `_write` 的清洗——人写的和模型写的
    落进同一列、同样回注提示词，清洗口径不能有两套。注入时仍按不可信块包裹（读侧不分来源）。
    事件只记宿主固定文案，不抄新旧文字。

    `call_name` 传 None 表示不改称呼（前端还没传这一栏时就是 None）；传字符串时同样先
    去 NUL 再截到 `MAX_CALL_NAME_LENGTH`，空串表示清掉称呼。这里截断不用
    `sanitize_inline_text`：`_write` 里还会再清洗一遍，两处都加省略号会出现「…….」。
    """
    snapshot, _outcome = await _write(
        key,
        delta=0,
        target_score=None,
        reason=REASON_NARRATIVE_EDIT,
        source=AffectionEventSource.NARRATIVE_EDIT,
        state_text=strip_nul(state_text)[:MAX_STATE_TEXT_LENGTH],
        event_text=strip_nul(last_event_text)[:MAX_EVENT_TEXT_LENGTH],
        operator=operator,
        bump_interaction=False,
        call_name=None if call_name is None else strip_nul(call_name)[:MAX_CALL_NAME_LENGTH],
    )
    return snapshot


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

    `clear_state_text=True` 是「重置」专用的：两段描述之外，称呼也一并清掉——
    称呼同样是模型写的字，重置要回到「这个人我一无所知」的状态。
    """
    snapshot, _outcome = await _write(
        key,
        delta=0,
        target_score=clamp_score(score),
        reason=reason,
        source=source,
        state_text="" if clear_state_text else None,
        event_text="" if clear_state_text else None,
        operator=operator,
        bump_interaction=False,
        call_name="" if clear_state_text else None,
    )
    return snapshot


async def set_call_name(
    key: AffectionKey,
    *,
    call_name: str,
    chat_key: str = "",
    nickname: str = "",
    source_platform_userid: str = "",
) -> AffectionSnapshot:
    """只改称呼：分数、阶段、两段描述与互动次数都不动

    记一条 `delta=0`、`source=tool` 的流水，理由是宿主固定文案——称呼正文不进流水，
    它已经原样存在 `call_name` 列上，抄进流水只是换个地方多留一份。

    `update_event_text=False` 是关键：否则「改了称呼」会写进 `last_event_text`，
    下一轮好感块的 `recently:` 就变成了这句宿主文案，把真正最近发生的事挤掉。

    **`last_interact_at` 不更新**：改称呼不算一次互动，久别提示按「最近一次有好感记录的
    互动」算，记个称呼不该让「好久不见」消失。因此这里也不累加 `interaction_count`。
    """
    snapshot, _outcome = await _write(
        key,
        delta=0,
        target_score=None,
        reason=REASON_CALL_NAME,
        source=AffectionEventSource.TOOL,
        chat_key=chat_key,
        nickname=nickname,
        state_text=None,
        event_text=None,
        bump_interaction=False,
        source_platform_userid=source_platform_userid,
        update_event_text=False,
        call_name=call_name,
    )
    return snapshot


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
    call_name: Optional[str] = None,
    guard: Optional[AdminWriteGuard] = None,
) -> Tuple[AffectionSnapshot, AffectionWriteOutcome]:
    """好感度的唯一写入口

    读改写放在同一个事务的行锁下：同一个人可能同时在多个群里说话，
    两条并发的评估各读到旧分数再各自写回，其中一次的变化就凭空消失了。
    行锁只能锁住已存在的行，因此建行动作提前到事务之外先做掉。

    **过滤与建行都用 `key.as_columns()` 这同一份列**：写进去的行自己查不到是这套五列键上
    最容易犯、也最难查的错（表现为每次写入都新建一行，分数永远停在第一次的增量上）。

    时间戳一律取 `tortoise.timezone.now()`：同表的 `create_time` 由 `auto_now_add` 写成
    带时区的值，这两列若写成朴素本地时间，同一列里就会混着两种表示，把"这条状态是什么时候
    生成的"变成一个要看是谁写的才能回答的问题。

    `guard` 的复核放在**拿到行锁之后**：后台写入走的也是这里、也要先拿同一把行锁，
    于是它要么已经提交（这里读得到它的事件，作废），要么排在本次写入后面（它会盖掉本次）。
    放在锁外复核会留下一个「复核完、写入前」的窗口，管理员恰好在那一刻点下去就会被盖回。

    `call_name` 传 None 表示不动这一列；传字符串则先剥 NUL 再按 `MAX_CALL_NAME_LENGTH` 清洗，
    空串表示清除——「不特别称呼」与「没改过」必须是两个状态，所以不能用 None 兼职。

    Returns:
        `(更新后的快照, 这次写入发生了什么)`。只有模型写分那条路径需要后半段
        （里程碑判据要用），其余入口只取快照——它们对外签名与返回值因此一个字都没变。
    """
    clean_reason = sanitize_inline_text(reason, MAX_EVENT_TEXT_LENGTH)
    clean_nickname = sanitize_inline_text(nickname, MAX_NICKNAME_LENGTH)
    clean_state = sanitize_block_text(state_text, MAX_STATE_TEXT_LENGTH) if state_text is not None else None
    clean_event = sanitize_inline_text(event_text, MAX_EVENT_TEXT_LENGTH) if event_text is not None else clean_reason
    clean_call_name = sanitize_inline_text(strip_nul(call_name), MAX_CALL_NAME_LENGTH) if call_name is not None else None

    await _ensure_record(key)

    async with in_transaction():
        record = await DBAffection.filter(**key.as_columns()).select_for_update().first()
        if record is None:  # pragma: no cover - `_ensure_record` 之后只有并发删除才会走到
            raise RuntimeError(f"好感度记录在写入前被删除: {key}")
        if guard is not None and await latest_admin_write_at(key) != guard.seen:
            raise AffectionWriteSuperseded(f"评估开始之后这一行有了新的后台写入，本次结果作废: {key}")

        # 改分**之前**取一次阶段：里程碑的「从」写的就是它。取在改分之后就只能靠
        # 反推 delta，而 target_score 那条路径上的 delta 是锁内才算出来的。
        stage_before = stage_of(record.score)
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
        if clean_call_name is not None:
            record.call_name = clean_call_name
        if chat_key:
            record.last_chat_key = chat_key[:256]
        if bump_interaction:
            record.interaction_count += 1
            record.last_interact_at = db_now()
        await record.save()

        event = await DBAffectionEvent.create(
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
    outcome = AffectionWriteOutcome(
        event_id=event.id,
        stage_before=stage_before,
        stage_after=stage_of(record.score),
        score_after=record.score,
    )
    invalidate_cache(key)
    return snapshot, outcome


async def list_events(key: AffectionKey, limit: int = 20) -> List[DBAffectionEvent]:
    """读取某个人最近的好感度变化事件（**这个人全部账号的并集**，按时间从新到旧）

    合并过的账号在流水里仍然是各自的 `platform_userid`（不物理搬行），因此这里必须按
    :func:`person_account_ids` 取并集。撤销合并后并集自然缩回去，两边的历史各自完好。

    排序用 `create_time` 而不是 `id`：后台呈现的是**时间线**，而 id 序只是"谁先被写进这张表"。
    单进程下两者一致，但这条流水会被多进程/多节点同时写（好感度评估是后台任务），
    也会被历史数据导入写入带旧时间戳的行——那时按 id 排会把一条更早发生的事显示在最上面。
    同一时刻的并列再按 id 兜底，保证同一份数据两次读出来的顺序相同。
    """
    return await (await _person_events(key)).order_by("-create_time", "-id").limit(limit)


async def _person_events(key: AffectionKey) -> QuerySet[DBAffectionEvent]:
    """这一行对应的那个人的全部流水：同域、同人设，人取全部账号的并集

    详情页的时间线（:func:`list_events`）、删一条流水的理由（:func:`redact_event_reason`）、
    后台写入的时间点（:func:`latest_admin_write_at`）都只认这一个范围。
    删理由那一条尤其要紧：它按 `event_id` 定位，范围一旦比详情页宽，就能拿别人的
    流水编号删到别的域、别的人设、别的人头上。
    """
    return DBAffectionEvent.filter(**key.scope_columns(), platform_userid__in=await person_account_ids(key))


async def list_milestone_events(key: AffectionKey, limit: int = 50) -> List[DBAffectionEvent]:
    """这个人这一份好感下生成过里程碑的流水，新的在前

    详情页的「关系里程碑」一栏用它，删除接口的归属校验也用它：一个段落要算「属于这一行」，
    判据只能是「这一行的某条流水把它生成出来」。范围与 :func:`list_events` 完全相同
    （同域、同人设、含合并过来的账号），否则后台能拿别人的段落编号删到别人的记忆。
    """
    return await (await _person_events(key)).filter(memory_paragraph_id__isnull=False).order_by("-id").limit(limit)


async def person_milestone_paragraph_ids(key: AffectionKey) -> List[int]:
    """这个人这一份好感下全部里程碑段落 ID，去重、按流水 ID 倒序

    重置要用它把这一份好感下的里程碑全部软删。去重是必要的：同一条段落理论上只由一条
    流水生成，但历史数据、重复调用都可能让两行指向同一段，软删两次不致命，
    可计数会骗人。顺序只为了让「先删新的」这件事确定下来，便于用例断言。
    """
    rows = (
        await (await _person_events(key))
        .filter(memory_paragraph_id__isnull=False)
        .order_by("-id")
        .values_list("memory_paragraph_id", flat=True)
    )
    seen: List[int] = []
    for row in rows:
        if row is None:
            continue
        paragraph_id = int(row)
        if paragraph_id not in seen:
            seen.append(paragraph_id)
    return seen


async def redact_event_reason(key: AffectionKey, event_id: int, *, operator: str) -> Optional[DBAffectionEvent]:
    """软删一条流水的理由：`reason` 置空，记下删除时间与操作者，行与分数一概不动

    Returns:
        处理后的事件；这条流水不在这一行那个人的范围里时返回 None（调用方按 404 处理）。

    已经删过的再删一次原样返回、不改删除记录：第一次是谁、什么时候删的才是要留的痕迹。
    `last_event_text` 跟流水是两回事，这里不连带它（要改它走 :func:`clear_narrative` /
    :func:`edit_narrative`）。快照里没有流水，但照样让缓存失效，与另外两个后台操作同一口径。

    这条流水生成过里程碑时，还要把那段改写成不带起因的模板：起因就抄自这条理由，
    理由删了正文却留着，等于换个地方原样保存了要删掉的内容。改写失败只告警——
    理由已经删掉这件事不能因为记忆那边出问题而回滚。
    """
    event = await (await _person_events(key)).filter(id=event_id).first()
    if event is None:
        return None
    if event.deleted_at is None:
        event.reason = ""
        event.deleted_at = db_now()
        event.deleted_by = operator[:128]
        await event.save(update_fields=["reason", "deleted_at", "deleted_by"])
    invalidate_cache(key)
    if event.memory_paragraph_id is not None:
        # 延迟导入：`milestones` 反过来要用本模块的读写入口，模块级导入会成环。
        from . import milestones

        try:
            await milestones.strip_reason(int(event.memory_paragraph_id), domain=key.domain)
        except Exception as e:
            logger.warning(f"删除流水理由后改写里程碑失败（已忽略）: event_id={event_id}, error={e}")
    return event


async def latest_admin_write_at(key: AffectionKey) -> Optional[datetime]:
    """这个人这一行最近一次后台写入（重置 / 只清印象 / 编辑印象）的时间；没有就是 None

    范围与详情页的时间线相同（同域、同人设、含合并过来的账号），来源见
    `stage.ADMIN_WRITE_SOURCES`。不新增列：流水本来就逐条记着来源与时间。
    """
    rows = (
        await (await _person_events(key))
        .filter(source__in=sorted(source.value for source in ADMIN_WRITE_SOURCES))
        .order_by("-create_time", "-id")
        .limit(1)
        .values_list("create_time", flat=True)
    )
    return rows[0] if rows else None


async def latest_admin_writes(keys: Iterable[AffectionKey]) -> Dict[AffectionKey, Optional[datetime]]:
    """批量取 :func:`latest_admin_write_at`，键去重；一轮评估的参与者通常只有几个人"""
    result: Dict[AffectionKey, Optional[datetime]] = {}
    for key in keys:
        if key not in result:
            result[key] = await latest_admin_write_at(key)
    return result


async def throttle_llm_delta(
    key: AffectionKey,
    *,
    delta: int,
    current_score: int,
    limits: AffectionRateLimits,
    now: Optional[datetime] = None,
) -> int:
    """模型写分专用（后台评估与主模型自主调整）：把变化量收进递减收益与滚动窗口预算

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
    """统计滚动窗口内由模型写分造成的累计涨跌

    只数模型写的分（后台评估与主模型自主调整，见 `stage.MODEL_SOURCES`）：人工调整、
    重置与账号合并的补偿都是运营手段，既不占用用户的额度，也不会因为管理员改了一次分
    就把额度"退"回来。

    两种模型来源必须**共用一份**日 / 周额度：否则模型可以靠「评估加一次、自己再调一次」
    把额度翻倍——同一段对话被算两次，正是这条额度要挡的事。

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
            source__in=[source.value for source in MODEL_SOURCES],
        )
        return AffectionWindowUsage(
            daily_gain=await _sum_deltas(events, since=daily_since, positive=True),
            daily_drop=await _sum_deltas(events, since=daily_since, positive=False),
            weekly_gain=await _sum_deltas(events, since=weekly_since, positive=True),
        )
    except Exception as e:
        logger.warning(f"好感度窗口用量统计失败（本轮不做窗口限制）: key={key}, error={e}")
        return AffectionWindowUsage.unused()


async def tool_adjusted_since(key: AffectionKey, *, chat_key: str, since: datetime) -> bool:
    """这个人在这段时间里有没有在本频道自己调过分

    评估用它判断「主模型这一轮是不是已经表过态了」：同一段对话被主模型当场调一次、
    再被后台评估判一次，就是同一件事算了两遍。改称呼那条 `delta = 0` 的流水不算——
    它没有动分数，评估该照常打分。

    范围与 :func:`window_usage` 相同（同域、同人设、这个人全部账号的并集）：
    并集若不一致，换个号说句话就能让评估再算一次。

    查询失败时返回 False（fail-open）：这条判断只是少算一次分，而额度那一层仍然兜底，
    不该因为一次统计查询失败就把这一轮评估整个丢掉。
    """
    return await _scored_since(key, chat_key=chat_key, since=since, source=AffectionEventSource.TOOL)


async def evaluator_scored_since(key: AffectionKey, *, chat_key: str, since: datetime) -> bool:
    """后台评估在这段时间里有没有在本频道给这个人记过分（:func:`tool_adjusted_since` 的另一半）

    评估在每轮开始时登记、与主模型并行跑：评估先落了分，主模型随后才当场调分，
    评估那一刻看不到工具流水，同一段对话就会被算两次。自主调分因此反过来查一次：
    `since` 取这个人在本频道最近一条发言的时间，那之后评估已经记过非零分，说明评估看的
    正是这一段，这次就不再记。两边合起来是「同一段对话只记一次，谁先写算谁的」。

    只看 `delta != 0`：评估因为工具已调过而只更新印象时，记的是 0 分流水，不算。
    口径、账号并集与失败处理同 :func:`tool_adjusted_since`。
    """
    return await _scored_since(key, chat_key=chat_key, since=since, source=AffectionEventSource.LLM)


async def _scored_since(
    key: AffectionKey,
    *,
    chat_key: str,
    since: datetime,
    source: AffectionEventSource,
) -> bool:
    try:
        return await DBAffectionEvent.filter(
            **key.scope_columns(),
            platform_userid__in=await person_account_ids(key),
            chat_key=chat_key,
            create_time__gte=since,
            source=source.value,
        ).exclude(delta=0).exists()
    except Exception as e:
        logger.warning(
            f"好感度模型记分查询失败（按没有记过处理）: key={key}, chat_key={chat_key}, source={source.value}, error={e}",
        )
        return False


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
