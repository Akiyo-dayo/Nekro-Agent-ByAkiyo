"""嵌入（向量化）调用的健康状态：长期失败时告警一次，恢复时记一笔

## 为什么在这一层记

嵌入的上游 Key 失效以后，记忆沉淀、召回、近重复检查、知识库各自把失败当成「这一条没向量」
吞掉，只记一条 WARNING：记忆照存、召回退回空结果、知识库退回关键词。单看每一处都是对的
（嵌入挂了不能拖垮对话），合起来就是「一天多、几百段记忆没有向量，没人发现」。

所以状态记在真正打上游的那一跳（`services/agent/openai.py::gen_openai_embeddings`）：记忆、
知识库、表情包插件都从这里过，各处的吞法不用动。每一次尝试记一笔（与用量流水同一个粒度），
模型组名与发起实例取自当前的用量归属声明（`usage_scope`）。模型组连通性测试打的可能是一份
没保存的草稿配置，用 :func:`untracked` 排除在外。

## 判据（按模型组）

- 一次成功就清零。
- **鉴权、额度、地址或模型名不对**（401 / 403、402 / 429、404 等其余 4xx）：不会自己好，连续失败
  :data:`FAST_FAILURE_COUNT` 次、且第一次到最近一次至少隔 :data:`FAST_FAILURE_MIN_SPAN_SECONDS`
  秒就判长期失败。要求隔开这一点是为了不让「一次调用里的几次重试」（几秒内）单独凑够次数。
- **网络、上游 5xx、回包形态不对**：可能自己好，从这一串失败的第一次起超过
  :data:`SLOW_FAILURE_SECONDS` 秒仍没有成功过一次（且至少失败 :data:`SLOW_FAILURE_COUNT` 次）才判。
- **跟这一条输入有关的拒绝**（:data:`INPUT_REJECTION_STATUSES`：400 / 413 / 422，常见于单条太长、
  格式不对、被内容审核拦下）：只记次数与时间，不进失败串、也不打断它。流量少的时候一两条长文本
  就能凑够「连续失败」，会误报「检查 Key 与地址」。代价是个别兼容接口把模型名不对也回 400，
  这种不会判长期失败；后台状态里「被拒的输入」计数会一直涨，看得出来。
- 判的是这一串里**最近一次**失败的类别。
- 跨度用单调时钟算，系统时间被校准、往回拨都不影响判定；展示用的时间另记墙上时间。

## 进入、恢复与不刷屏

进入长期失败时记一条 ERROR、发一次提醒；之后再失败只更新计数，不再记、不再发。恢复（下一次
成功）时记一条 INFO。同一个模型组两次提醒之间至少隔 :data:`REALERT_MIN_SECONDS`：好了又坏、
坏了又好的抖动不会每次都发。状态只在进程内存里：重启后仍在失败的话会重新判、再提醒一次。
记的模型组满了先丢不在长期失败、近期也没提醒过的，正在告警的组连同它上次提醒的时间留到最后。

## 谁收到提醒

归属在进入长期失败的那一刻查一次库、记进状态，提醒与后台列表都用这个值（恢复时清掉，下一次进入重新查）：

- 模型组归平台方：走 `dispatch_notification` 的全局渠道（平台方邮箱、平台方 Webhook、运维频道）。
- 模型组归某个租户：只发给这个租户自己实例层上配的告警 Webhook（`scoped_only`），按地址去重；
  不发平台方，也不发别的租户——哪怕别的租户的实例也在用这个组。租户一条都没配就只能在后台看到。
- 查不到（组已经删了）或查库出错：不发，只记一条 WARNING。宁可不发，也不把带着租户自起组名的
  提醒当成平台方的发到全局渠道。

## 不带 Key

日志、提醒正文、后台状态里只有类别、HTTP 状态码与异常类名，**不放任何错误原文**：供应商在
4xx 体里回显 `Incorrect API key provided: sk-…` 是常见形态，脱敏判据再全也是一道可以漏的闸，
不放原文就没有可漏的东西。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

import httpx

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("embedding_health")

# 鉴权、额度、地址或模型名不对：连续失败这么多次……
FAST_FAILURE_COUNT = 3
# ……且第一次到最近一次至少隔这么久（秒），才判长期失败
FAST_FAILURE_MIN_SPAN_SECONDS = 60.0
# 网络、上游 5xx、回包不对：这一串失败持续这么久（秒）仍没有成功过一次……
SLOW_FAILURE_SECONDS = 30 * 60.0
# ……且至少失败这么多次，才判长期失败
SLOW_FAILURE_COUNT = 3
# 同一个模型组两次提醒之间至少隔这么久（秒）
REALERT_MIN_SECONDS = 6 * 3600.0
# 记多少个模型组的状态；满了先丢不在告警的（见 `_evict_one`）
MAX_TRACKED_GROUPS = 256
# 每个模型组记多少个最近的发起实例（租户提醒按它选收件的实例）
MAX_REQUESTERS_PER_GROUP = 32
# 租户提醒最多试多少个实例的告警 Webhook
MAX_TENANT_ALERT_INSTANCES = 50
# 跟这一条输入有关的 4xx：太长（413）、格式或内容不对（400 / 422，内容审核拦下多半也是 400）
INPUT_REJECTION_STATUSES = frozenset({400, 413, 422})
# 缺向量段数在进程里缓存这么久（秒）：后台每分钟读一次，提醒也要它，不必每次都数一遍全表
MISSING_VECTORS_CACHE_SECONDS = 60.0
# 数缺向量时，长度不超过这么多字的段落再取出正文看是不是全是空白（与补向量命令跳过空正文同一口径）
BLANK_PROBE_MAX_CHARS = 256

KIND_AUTH = "auth"
KIND_QUOTA = "quota"
KIND_REJECTED = "rejected"
KIND_SERVER = "server"
KIND_NETWORK = "network"
KIND_INPUT = "input"
FAST_KINDS = frozenset({KIND_AUTH, KIND_QUOTA, KIND_REJECTED})

KIND_LABELS: Dict[str, str] = {
    KIND_AUTH: "鉴权失败（Key 无效或没有权限）",
    KIND_QUOTA: "额度不足或被限流",
    KIND_REJECTED: "请求被上游拒绝（地址或模型名不对）",
    KIND_SERVER: "上游出错或回包形态不对",
    KIND_NETWORK: "网络不通（连不上、超时或代理出错）",
    KIND_INPUT: "这一条输入被拒（太长、格式不对或被内容审核拦下）",
}

# 恢复与提醒里给的「怎么补缺失的向量」：超级用户命令，补的是失败期间沉淀、`embedding_ref` 为空的记忆段
BACKFILL_HINT = "失败期间沉淀的记忆缺向量，恢复后可用超级用户命令 memory_backfill_vectors 补齐。"

STATE_OK = "ok"
STATE_UNSTABLE = "unstable"
STATE_FAILING = "failing"

OWNER_PLATFORM = "platform"
OWNER_TENANT = "tenant"
# 库里查不到这个模型组（多半是已经删了）
OWNER_UNKNOWN = "unknown"

_untracked: ContextVar[bool] = ContextVar("embedding_health_untracked", default=False)


@contextmanager
def untracked() -> Iterator[None]:
    """这一块里的嵌入调用不计入健康状态（模型组连通性测试用：它打的可能是没保存的草稿配置）"""
    token = _untracked.set(True)
    try:
        yield
    finally:
        _untracked.reset(token)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(exc: BaseException) -> Optional[int]:
    for item in _exception_chain(exc):
        response = getattr(item, "response", None)
        code = getattr(response, "status_code", None) if response is not None else None
        if code is None:
            code = getattr(item, "status_code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code
    return None


def _is_network(exc: BaseException) -> bool:
    return any(
        isinstance(item, (httpx.TransportError, ConnectionError, TimeoutError, asyncio.TimeoutError, OSError))
        for item in _exception_chain(exc)
    )


def classify_error(exc: BaseException) -> Tuple[str, Optional[int]]:
    """失败归类：`(类别, HTTP 状态码)`；没有状态码时第二项为 None"""
    status = _status_code(exc)
    if status is not None:
        if status in (401, 403):
            return KIND_AUTH, status
        if status in (402, 429):
            return KIND_QUOTA, status
        if status in INPUT_REJECTION_STATUSES:
            return KIND_INPUT, status
        if 400 <= status < 500 and status != 408:
            return KIND_REJECTED, status
        return KIND_SERVER, status
    if _is_network(exc):
        return KIND_NETWORK, None
    return KIND_SERVER, None


def _iso(moment: Optional[float]) -> Optional[str]:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()


def _local(moment: Optional[float]) -> str:
    if moment is None:
        return ""
    return datetime.fromtimestamp(moment).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


@dataclass(frozen=True)
class EmbeddingHealth:
    """一个模型组此刻的嵌入健康状态（只读快照）；时间都是墙上时间（秒）"""

    model_group: str
    state: str
    consecutive_failures: int
    kind: str
    status_code: Optional[int]
    error_type: str
    streak_started_at: Optional[float]
    last_failure_at: Optional[float]
    last_success_at: Optional[float]
    failing_since: Optional[float]
    # 进入长期失败时查到的归属；还没查（或不在长期失败）为空串
    owner: str = ""
    owner_tenant_id: Optional[int] = None
    # 跟输入有关、不计入失败串的拒绝：本次启动以来的次数与最近一次
    input_rejections: int = 0
    last_input_rejection_at: Optional[float] = None
    last_input_status: Optional[int] = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def describe_error(self) -> str:
        """「类别（HTTP 状态码）」；只有这些，不带任何错误原文"""
        if not self.kind:
            return ""
        if self.status_code is not None:
            return f"{self.kind_label}（HTTP {self.status_code}）"
        if self.error_type:
            return f"{self.kind_label}（{self.error_type}）"
        return self.kind_label


@dataclass
class _GroupState:
    consecutive_failures: int = 0
    # 墙上时间只拿来展示；判跨度、判提醒间隔、挑淘汰对象一律用单调时钟（`*_mono`）
    streak_started_at: Optional[float] = None
    streak_started_mono: Optional[float] = None
    last_failure_at: Optional[float] = None
    last_success_at: Optional[float] = None
    kind: str = ""
    status_code: Optional[int] = None
    error_type: str = ""
    failing: bool = False
    failing_since: Optional[float] = None
    # 也是这一次长期失败的标识：查归属回来时用它确认还是同一次
    failing_since_mono: Optional[float] = None
    last_alert_mono: Optional[float] = None
    owner: str = ""
    owner_tenant_id: Optional[int] = None
    input_rejections: int = 0
    last_input_rejection_at: Optional[float] = None
    last_input_status: Optional[int] = None
    requesters: Dict[Tuple[str, str], float] = field(default_factory=dict)
    touched_mono: float = 0.0


AlertSender = Callable[[EmbeddingHealth, Tuple[Tuple[str, str], ...]], Awaitable[Any]]
OwnerResolver = Callable[[str], Awaitable[Tuple[str, Optional[int]]]]


class EmbeddingHealthTracker:
    """按模型组记嵌入调用的成败，判长期失败；判据见模块说明"""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._monotonic = monotonic
        self._groups: Dict[str, _GroupState] = {}
        self._alert_tasks: set[asyncio.Task[Any]] = set()
        # 不填就用 :func:`send_alert` 与 :func:`_owner_of`（用的时候再取，用例可以换掉）
        self.alert_sender: Optional[AlertSender] = None
        self.owner_resolver: Optional[OwnerResolver] = None

    # ==== 记 ====

    def _evict_one(self, mono: float) -> None:
        """满了丢一个：先丢不在长期失败、近期也没提醒过的，同档里丢最久没动的

        正在告警的组被丢掉，下一次失败就从零记起、凑够了再提醒一次；近期提醒过的组被丢掉，
        它的 `last_alert_mono` 跟着没了，抖动防护就失效。这两种都留到最后。
        """

        def keep_priority(name: str) -> Tuple[bool, bool, float]:
            state = self._groups[name]
            alerted_recently = state.last_alert_mono is not None and mono - state.last_alert_mono < REALERT_MIN_SECONDS
            return state.failing, alerted_recently, state.touched_mono

        victim = min(self._groups, key=keep_priority)
        self._groups.pop(victim, None)

    def _state(self, model_group: str, mono: float) -> _GroupState:
        state = self._groups.get(model_group)
        if state is None:
            if len(self._groups) >= MAX_TRACKED_GROUPS:
                self._evict_one(mono)
            state = self._groups[model_group] = _GroupState()
        state.touched_mono = mono
        return state

    def record_success(self, model_group: str) -> None:
        """一次成功：清零；正处在长期失败里就记一条恢复"""
        if not model_group:
            return
        mono = self._monotonic()
        state = self._state(model_group, mono)
        if state.failing:
            started = state.streak_started_mono if state.streak_started_mono is not None else mono
            logger.info(
                f"嵌入调用已恢复：模型组 {model_group}，这次失败持续了 {_duration(mono - started)}、"
                f"期间失败 {state.consecutive_failures} 次。{BACKFILL_HINT}",
            )
        state.consecutive_failures = 0
        state.streak_started_at = None
        state.streak_started_mono = None
        state.failing = False
        state.failing_since = None
        state.failing_since_mono = None
        # 归属只对这一次长期失败有效：组可能删了又被别人用同一个名字建
        state.owner = ""
        state.owner_tenant_id = None
        state.last_success_at = self._clock()

    def record_failure(
        self,
        model_group: str,
        exc: BaseException,
        *,
        adapter_key: str = "",
        instance_key: str = "",
    ) -> None:
        """一次失败：计数、归类；刚跨进长期失败时记一条 ERROR，查归属并发一次提醒"""
        if not model_group:
            return
        mono = self._monotonic()
        wall = self._clock()
        state = self._state(model_group, mono)
        kind, status = classify_error(exc)
        if kind == KIND_INPUT:
            # 跟这一条输入有关：记下来给后台看，不进失败串、也不打断它
            state.input_rejections += 1
            state.last_input_rejection_at = wall
            state.last_input_status = status
            return
        if state.consecutive_failures == 0 or state.streak_started_mono is None:
            state.streak_started_at = wall
            state.streak_started_mono = mono
        state.consecutive_failures += 1
        state.last_failure_at = wall
        state.kind = kind
        state.status_code = status
        state.error_type = type(exc).__name__
        if adapter_key and instance_key:
            state.requesters.pop((adapter_key, instance_key), None)
            state.requesters[(adapter_key, instance_key)] = mono
            while len(state.requesters) > MAX_REQUESTERS_PER_GROUP:
                state.requesters.pop(next(iter(state.requesters)))

        if state.failing or not self._crossed(state, mono):
            return
        state.failing = True
        state.failing_since = wall
        state.failing_since_mono = mono
        health = self._snapshot(model_group, state)
        last_success = _local(state.last_success_at) or "本次启动以来没有成功过"
        logger.error(
            f"嵌入调用长期失败：模型组 {model_group}，{health.describe_error()}；从 {_local(state.streak_started_at)} "
            f"起连续失败 {state.consecutive_failures} 次，最近一次成功：{last_success}。这段时间新沉淀的记忆没有向量，"
            "召回与近重复检查用不上它们，知识库检索退回关键词。修好以后自动恢复，期间不再重复记这一条",
        )
        alert = state.last_alert_mono is None or mono - state.last_alert_mono >= REALERT_MIN_SECONDS
        if alert:
            state.last_alert_mono = mono
        else:
            logger.info(f"模型组 {model_group} 距上次嵌入失败提醒不到 {_duration(REALERT_MIN_SECONDS)}，这次不再发提醒")
        requesters = tuple(reversed(list(state.requesters)))
        self._schedule_entered(model_group, mono, health, requesters, alert=alert)

    @staticmethod
    def _crossed(state: _GroupState, mono: float) -> bool:
        started = state.streak_started_mono if state.streak_started_mono is not None else mono
        span = mono - started
        if state.kind in FAST_KINDS:
            return state.consecutive_failures >= FAST_FAILURE_COUNT and span >= FAST_FAILURE_MIN_SPAN_SECONDS
        return state.consecutive_failures >= SLOW_FAILURE_COUNT and span >= SLOW_FAILURE_SECONDS

    def _schedule_entered(
        self,
        model_group: str,
        episode: float,
        health: EmbeddingHealth,
        requesters: Tuple[Tuple[str, str], ...],
        *,
        alert: bool,
    ) -> None:
        """刚进入长期失败：查一次归属记进状态，需要的话按这个归属发提醒（都在后台任务里）"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"模型组 {model_group} 嵌入长期失败，但当前没有事件循环，归属没查、提醒没有发出")
            return

        resolver = self.owner_resolver or _owner_of
        sender = self.alert_sender or send_alert

        async def run() -> None:
            try:
                owner, tenant_id = await resolver(model_group)
            except Exception as exc:  # noqa: BLE001 - 查不到就按归属不明处理（不发），不能反过来打掉业务
                logger.warning(f"查模型组 {model_group} 的归属出错（{type(exc).__name__}），这次按归属不明处理")
                owner, tenant_id = "", None
            self._remember_owner(model_group, episode, owner, tenant_id)
            if not alert:
                return
            try:
                await sender(replace(health, owner=owner, owner_tenant_id=tenant_id), requesters)
            except Exception as exc:  # noqa: BLE001 - 提醒是观测，不能反过来打掉业务
                logger.warning(f"嵌入失败提醒投递出错（已忽略）: model_group={model_group} | {type(exc).__name__}")

        task = loop.create_task(run())
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    def _remember_owner(self, model_group: str, episode: float, owner: str, tenant_id: Optional[int]) -> None:
        """把查到的归属记到这一次长期失败上；查的工夫里已经恢复（或换了一次）就不记"""
        state = self._groups.get(model_group)
        if not owner or state is None or not state.failing or state.failing_since_mono != episode:
            return
        state.owner = owner
        state.owner_tenant_id = tenant_id

    async def drain_alerts(self) -> None:
        """等已经排上的归属查询与提醒做完（用例与关停时用）"""
        while self._alert_tasks:
            await asyncio.gather(*list(self._alert_tasks), return_exceptions=True)

    # ==== 读 ====

    def _snapshot(self, model_group: str, state: _GroupState) -> EmbeddingHealth:
        if state.failing:
            current = STATE_FAILING
        elif state.consecutive_failures:
            current = STATE_UNSTABLE
        else:
            current = STATE_OK
        return EmbeddingHealth(
            model_group=model_group,
            state=current,
            consecutive_failures=state.consecutive_failures,
            kind=state.kind if state.consecutive_failures else "",
            status_code=state.status_code if state.consecutive_failures else None,
            error_type=state.error_type if state.consecutive_failures else "",
            streak_started_at=state.streak_started_at,
            last_failure_at=state.last_failure_at,
            last_success_at=state.last_success_at,
            failing_since=state.failing_since,
            owner=state.owner,
            owner_tenant_id=state.owner_tenant_id,
            input_rejections=state.input_rejections,
            last_input_rejection_at=state.last_input_rejection_at,
            last_input_status=state.last_input_status,
        )

    def snapshot(self, model_group: str) -> Optional[EmbeddingHealth]:
        state = self._groups.get(model_group)
        return None if state is None else self._snapshot(model_group, state)

    def snapshots(self) -> List[EmbeddingHealth]:
        return [self._snapshot(name, state) for name, state in sorted(self._groups.items())]

    def is_failing(self, model_group: str) -> bool:
        state = self._groups.get(model_group)
        return bool(state is not None and state.failing)

    def reset(self) -> None:
        """只给用例用：清空全部状态"""
        self._groups.clear()


tracker = EmbeddingHealthTracker()


def _current_attribution() -> Tuple[str, str, str]:
    from nekro_agent.services.usage.context import current_usage_context

    context = current_usage_context()
    return context.model_group_name, context.adapter_key, context.instance_key


def note_success() -> None:
    """`gen_openai_embeddings` 成功后调用；模型组取自当前的用量归属声明。不抛异常"""
    if _untracked.get():
        return
    try:
        model_group, _adapter, _instance = _current_attribution()
        tracker.record_success(model_group)
    except Exception as exc:  # noqa: BLE001 - 记状态失败不能让一次成功的调用变成失败
        logger.debug(f"记录嵌入成功状态出错（已忽略）: {type(exc).__name__}")


def note_failure(exc: BaseException) -> None:
    """`gen_openai_embeddings` 失败后调用（调用方随后照常抛出原异常）。不抛异常"""
    if _untracked.get():
        return
    try:
        model_group, adapter_key, instance_key = _current_attribution()
        tracker.record_failure(model_group, exc, adapter_key=adapter_key, instance_key=instance_key)
    except Exception as error:  # noqa: BLE001
        logger.debug(f"记录嵌入失败状态出错（已忽略）: {type(error).__name__}")


# ==== 提醒 ====


def render_alert(health: EmbeddingHealth, *, missing_vectors: Optional[int] = None) -> Tuple[str, str]:
    """提醒的标题与正文（纯文本）；只用快照里的字段，不带任何错误原文

    `missing_vectors` 是全库缺向量的记忆段数，只有发给平台方的那一份带（租户那一份不该看到全平台的数）。
    """
    title = f"嵌入（向量化）持续失败：模型组 {health.model_group}"
    last_success = _local(health.last_success_at) or "本次启动以来没有成功过"
    lines = [
        f"模型组：{health.model_group}",
        f"错误类别：{health.describe_error()}",
        f"从 {_local(health.streak_started_at)} 起连续失败 {health.consecutive_failures} 次，"
        f"最近一次失败 {_local(health.last_failure_at)}",
        f"最近一次成功：{last_success}",
        "影响：这段时间新沉淀的记忆没有向量，向量召回与近重复检查用不上它们；知识库检索退回关键词，入库会失败。",
    ]
    if missing_vectors is not None:
        lines.append(f"目前有 {missing_vectors} 段有效的记忆缺向量。")
    lines.append(
        f"处理：检查模型组「{health.model_group}」的 API Key、额度与网络。修好以后自动恢复，不必重启；"
        f"这次失败期间不再重复提醒。{BACKFILL_HINT}",
    )
    return title, "\n".join(lines)


_missing_cache: Optional[Tuple[float, int]] = None
# 缓存用的时钟（单调）；用例换掉它来模拟过期
_cache_clock: Callable[[], float] = time.monotonic


def clear_missing_vectors_cache() -> None:
    """丢掉缓存的缺向量段数（用例用；补完向量想立刻看到新数时也可以调）"""
    global _missing_cache
    _missing_cache = None


def _memory_embedding_group() -> str:
    from nekro_agent.core.config import config

    return str(getattr(config, "MEMORY_EMBEDDING_MODEL_GROUP", "") or "").strip()


async def _count_missing_vectors_now() -> int:
    from tortoise.functions import Length

    from nekro_agent.models.db_mem_paragraph import DBMemParagraph

    total = await DBMemParagraph.filter(is_inactive=False, embedding_ref__isnull=True).count()
    if total == 0:
        return 0
    # 正文全是空白的段落补向量命令会跳过（补不了），不算「等着补」。SQL 的 TRIM 只去空格不去换行与制表符，
    # 所以只把短的那些取出来用 `str.strip` 判；全是空白又长过 BLANK_PROBE_MAX_CHARS 字的段落实际不会有
    short = (
        await DBMemParagraph.filter(is_inactive=False, embedding_ref__isnull=True)
        .annotate(chars=Length("content"))
        .filter(chars__lte=BLANK_PROBE_MAX_CHARS)
        .values_list("content", flat=True)
    )
    blank = sum(1 for text in short if not str(text or "").strip())
    return max(0, total - blank)


async def count_missing_vectors() -> Optional[int]:
    """有效（没失活）、正文不是空白、却没有向量的记忆段数；没配记忆嵌入模型组或查不了时为 None

    嵌入失败期间沉淀的记忆照存，只是 `embedding_ref` 为空，这个数就是「有多少段等着补向量」。
    只给平台方看：它是全平台的数。在进程里缓存 :data:`MISSING_VECTORS_CACHE_SECONDS` 秒，
    查不了的那一次不缓存。没配记忆嵌入模型组时补不了，也就不给数（后台不显示那条蓝条）。
    """
    global _missing_cache
    if not _memory_embedding_group():
        return None
    now = _cache_clock()
    cached = _missing_cache
    if cached is not None and 0 <= now - cached[0] < MISSING_VECTORS_CACHE_SECONDS:
        return cached[1]
    try:
        count = await _count_missing_vectors_now()
    except Exception as exc:  # noqa: BLE001 - 一个附带的数，查不了不能让状态页或提醒失败
        logger.debug(f"统计缺向量的记忆段失败（已忽略）: {type(exc).__name__}")
        return None
    _missing_cache = (now, count)
    return count


async def _owner_of(model_group: str) -> Tuple[str, Optional[int]]:
    """模型组此刻归谁：`(OWNER_PLATFORM, None)`、`(OWNER_TENANT, 租户)` 或库里查不到时 `(OWNER_UNKNOWN, None)`"""
    from nekro_agent.models.db_model_group import DBModelGroup

    row = await DBModelGroup.get_or_none(name=model_group)
    if row is None:
        return OWNER_UNKNOWN, None
    if row.is_platform_owned:
        return OWNER_PLATFORM, None
    if row.owner_tenant_id is None:
        return OWNER_UNKNOWN, None
    return OWNER_TENANT, row.owner_tenant_id


async def _tenant_instances(tenant_id: int, requesters: Tuple[Tuple[str, str], ...]) -> List[Tuple[str, str]]:
    """这个租户名下、按「最近用过这个组的在前」排好的实例"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    rows = await DBAdapterInstance.filter(tenant_id=tenant_id).order_by("id").values_list("adapter_key", "instance_key")
    owned = [(str(adapter), str(instance)) for adapter, instance in rows]
    owned_set = set(owned)
    ordered = [pair for pair in requesters if pair in owned_set]
    ordered.extend(pair for pair in owned if pair not in ordered)
    return ordered[:MAX_TENANT_ALERT_INSTANCES]


async def send_alert(health: EmbeddingHealth, requesters: Tuple[Tuple[str, str], ...]) -> List[Tuple[str, str, str]]:
    """按快照里记下的归属发一次提醒；返回发到了哪里（`("platform", "", "")` 或 `("tenant", 适配器, 实例)`）

    归属不在这里再查：进入长期失败时已经查过、记在快照上（见模块说明）。归属不明时不发。
    """
    from nekro_agent.services.notification import channels

    if health.owner == OWNER_PLATFORM:
        title, body = render_alert(health, missing_vectors=await count_missing_vectors())
        await channels.dispatch_notification(title, body, "error")
        return [(OWNER_PLATFORM, "", "")]

    tenant_id = health.owner_tenant_id
    if health.owner != OWNER_TENANT or tenant_id is None:
        logger.warning(
            f"模型组 {health.model_group} 嵌入长期失败，但查不到它归谁（组可能已经删了），这次不发提醒；"
            "平台方在后台的嵌入状态里看得到",
        )
        return []

    title, body = render_alert(health)

    sent: List[Tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for adapter_key, instance_key in await _tenant_instances(tenant_id, requesters):
        # 只认写在实例层上的那条：回落到全局 / 适配器层的地址是平台方的
        if not channels.alert_webhook_is_instance_owned(adapter_key, instance_key):
            continue
        url = channels.resolve_alert_config(adapter_key, instance_key).INSTANCE_ALERT_WEBHOOK_URL.strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        await channels.dispatch_notification(
            title,
            body,
            "error",
            adapter_key=adapter_key,
            instance_key=instance_key,
            scoped_only=True,
        )
        sent.append((OWNER_TENANT, adapter_key, instance_key))
    if not sent:
        logger.warning(
            f"租户 {tenant_id} 的模型组 {health.model_group} 嵌入长期失败，但这个租户的实例上没有配告警 Webhook，"
            "只能在后台的嵌入状态里看到",
        )
    return sent


# ==== 后台读 ====


def _uses_of(model_group: str) -> List[str]:
    from nekro_agent.core.config import config

    uses: List[str] = []
    if model_group and model_group == str(getattr(config, "MEMORY_EMBEDDING_MODEL_GROUP", "") or ""):
        uses.append("memory")
    if model_group and model_group == str(getattr(config, "KB_EMBEDDING_MODEL_GROUP", "") or ""):
        uses.append("kb")
    return uses


async def list_health(*, scope_tenant_id: Optional[int]) -> List[Dict[str, Any]]:
    """后台看的状态列表

    在长期失败里的组用进入时记下的归属（与提醒发给谁一致；那时查不到的就一直按归属不明，只给平台方），
    其余的现查。

    Args:
        scope_tenant_id: None 为平台方（全部模型组，含库里查不到的）；租户只看归自己的模型组——
            平台方的组、别的租户的组、归属不明的组一概不给（组名、失败时间都算别人的运营信息）。
    """
    items: List[Dict[str, Any]] = []
    for health in tracker.snapshots():
        if health.owner:
            owner, owner_tenant_id = health.owner, health.owner_tenant_id
        else:
            owner, owner_tenant_id = await _owner_of(health.model_group)
        if scope_tenant_id is not None and (owner != OWNER_TENANT or owner_tenant_id != scope_tenant_id):
            continue
        items.append(
            {
                "model_group": health.model_group,
                "owner": owner,
                "uses": _uses_of(health.model_group),
                "state": health.state,
                "consecutive_failures": health.consecutive_failures,
                "error_kind": health.kind,
                "status_code": health.status_code,
                "error_type": health.error_type,
                "streak_started_at": _iso(health.streak_started_at),
                "failing_since": _iso(health.failing_since),
                "last_failure_at": _iso(health.last_failure_at),
                "last_success_at": _iso(health.last_success_at),
                "input_rejections": health.input_rejections,
                "last_input_rejection_at": _iso(health.last_input_rejection_at),
                "last_input_status": health.last_input_status,
            },
        )
    return items


__all__ = [
    "FAST_FAILURE_COUNT",
    "FAST_FAILURE_MIN_SPAN_SECONDS",
    "INPUT_REJECTION_STATUSES",
    "KIND_LABELS",
    "MISSING_VECTORS_CACHE_SECONDS",
    "REALERT_MIN_SECONDS",
    "SLOW_FAILURE_COUNT",
    "SLOW_FAILURE_SECONDS",
    "EmbeddingHealth",
    "EmbeddingHealthTracker",
    "classify_error",
    "clear_missing_vectors_cache",
    "count_missing_vectors",
    "list_health",
    "note_failure",
    "note_success",
    "render_alert",
    "send_alert",
    "tracker",
    "untracked",
]
