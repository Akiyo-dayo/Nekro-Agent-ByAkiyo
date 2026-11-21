"""嵌入（向量化）调用的长期失败判定与提醒

要钉住的事：

1. 归类：401 / 403 鉴权、402 / 429 额度、404 等其余 4xx 被拒，这三类按次数判；网络、5xx、回包不对按时长判；
   400 / 413 / 422 跟这一条输入有关，只记次数，不进失败串也不打断它；
2. 阈值：按次数的要连续失败够次数、且跨度至少一分钟（一次调用里的几次重试凑不够）；按时长的要
   持续够久仍没成功过一次；跨度按单调时钟算，系统时间跳动不影响；
3. 进入长期失败时记一条 ERROR、提醒一次，之后不再记不再发；恢复时记一条 INFO；好了又坏的抖动在
   间隔内不重复提醒；记满了先丢不在告警的组；
4. 日志、提醒正文、后台状态里都不带 Key（错误原文一个字都不放）；
5. 归属在进入长期失败时查定、记进状态：平台方的组只提醒平台方；租户的组只发这个租户自己实例层的
   告警 Webhook，按地址去重，不发平台方、不发别的租户；之后组被删了也照这个归属；进入时就查不到的
   谁都不发；后台列表租户只看归自己的组；
6. 缺向量的段数：只数有效、正文不是空白的；缓存一分钟；没配记忆嵌入模型组时不给；
7. 记在 `gen_openai_embeddings` 这一跳：模型组与发起实例取自用量归属声明；连通性测试不计入。
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator, List, Optional, Tuple

import httpx
import pytest
from loguru import logger
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import health as health_router
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import embedding_health
from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.embedding_health import (
    FAST_FAILURE_COUNT,
    FAST_FAILURE_MIN_SPAN_SECONDS,
    REALERT_MIN_SECONDS,
    SLOW_FAILURE_SECONDS,
    EmbeddingHealthTracker,
    classify_error,
)
from nekro_agent.services.notification import channels
from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope
from nekro_agent.services.user.role import Role

SECRET = "sk-embedding-secret-0123456789abcdef"
REQUEST = httpx.Request("POST", "https://upstream.invalid/v1/embeddings")


def status_error(code: int, text: str = "") -> httpx.HTTPStatusError:
    """上游回了一个 4xx / 5xx；正文与异常消息里都回显着 Key（供应商的常见形态）"""
    response = httpx.Response(
        code, request=REQUEST, text=text or f'{{"error": "Incorrect API key provided: {SECRET}"}}'
    )
    return httpx.HTTPStatusError(f"{code} for key {SECRET}", request=REQUEST, response=response)


class Clock:
    """`now` 是单调时钟；墙上时间是 `now + wall_offset`（模拟系统时间被校准、往回拨）"""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.wall_offset = 0.0

    def __call__(self) -> float:
        return self.now + self.wall_offset

    def mono(self) -> float:
        return self.now


class Sent:
    def __init__(self) -> None:
        self.calls: List[Tuple[Any, Tuple[Tuple[str, str], ...]]] = []

    async def __call__(self, health: Any, requesters: Tuple[Tuple[str, str], ...]) -> None:
        self.calls.append((health, requesters))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sent() -> Sent:
    return Sent()


async def platform_owner(_model_group: str) -> Tuple[str, Optional[int]]:
    return "platform", None


@pytest.fixture
def tracker(clock: Clock, sent: Sent) -> EmbeddingHealthTracker:
    item = EmbeddingHealthTracker(clock=clock, monotonic=clock.mono)
    item.alert_sender = sent
    item.owner_resolver = platform_owner
    return item


@pytest.fixture
def logged() -> Iterator[List[Tuple[str, str]]]:
    records: List[Tuple[str, str]] = []
    sink_id = logger.add(lambda message: records.append((message.record["level"].name, message.record["message"])))
    yield records
    logger.remove(sink_id)


def levels(records: List[Tuple[str, str]], level: str, needle: str) -> List[str]:
    return [message for name, message in records if name == level and needle in message]


def fail(tracker: EmbeddingHealthTracker, clock: Clock, at: float, exc: BaseException, group: str = "emb") -> None:
    clock.now = 1_000_000.0 + at
    tracker.record_failure(group, exc, adapter_key="onebot_v11", instance_key="inst1")


class TestClassify:
    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            (401, "auth"),
            (403, "auth"),
            (402, "quota"),
            (429, "quota"),
            (400, "input"),
            (413, "input"),
            (422, "input"),
            (404, "rejected"),
            (405, "rejected"),
            (408, "server"),
            (500, "server"),
            (503, "server"),
        ],
    )
    def test_status_codes(self, code: int, kind: str) -> None:
        assert classify_error(status_error(code)) == (kind, code)

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("refused", request=REQUEST),
            httpx.ReadTimeout("slow", request=REQUEST),
            httpx.ProxyError("proxy"),
            ConnectionResetError("reset"),
            TimeoutError(),
        ],
    )
    def test_network_errors(self, exc: BaseException) -> None:
        assert classify_error(exc) == ("network", None)

    def test_a_malformed_body_is_an_upstream_problem(self) -> None:
        assert classify_error(KeyError("data")) == ("server", None)

    def test_the_status_is_found_through_the_cause_chain(self) -> None:
        try:
            try:
                raise status_error(401)
            except httpx.HTTPStatusError as inner:
                raise RuntimeError("嵌入失败") from inner
        except RuntimeError as outer:
            assert classify_error(outer) == ("auth", 401)


class TestThresholds:
    async def test_auth_needs_enough_failures_spread_over_a_minute(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
    ) -> None:
        """三次重试在几秒内连着失败不算长期失败：一次调用里的重试凑不够跨度"""
        for at in (0, 1, 2, 3):
            fail(tracker, clock, at, status_error(401))
        assert tracker.snapshot("emb").state == "unstable"

        fail(tracker, clock, FAST_FAILURE_MIN_SPAN_SECONDS, status_error(401))
        health = tracker.snapshot("emb")
        assert health.state == "failing"
        assert (health.kind, health.status_code, health.consecutive_failures) == ("auth", 401, 5)
        await tracker.drain_alerts()
        assert len(sent.calls) == 1

    async def test_two_failures_over_a_long_time_are_not_enough(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
    ) -> None:
        fail(tracker, clock, 0, status_error(403))
        fail(tracker, clock, 3600, status_error(403))
        assert FAST_FAILURE_COUNT == 3
        assert tracker.snapshot("emb").state == "unstable"

    @pytest.mark.parametrize("code", [402, 429, 404])
    async def test_quota_and_rejections_are_judged_like_auth(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        code: int,
    ) -> None:
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(code))
        assert tracker.snapshot("emb").state == "failing"

    async def test_network_failures_are_judged_by_duration(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
    ) -> None:
        error = httpx.ConnectError("refused", request=REQUEST)
        for at in (0, 60, 120, 600, SLOW_FAILURE_SECONDS - 1):
            fail(tracker, clock, at, error)
        assert tracker.snapshot("emb").state == "unstable", "半小时内没成功过也还不算：网络可能自己好"

        fail(tracker, clock, SLOW_FAILURE_SECONDS, error)
        health = tracker.snapshot("emb")
        assert health.state == "failing" and health.kind == "network" and health.status_code is None
        await tracker.drain_alerts()
        assert len(sent.calls) == 1

    async def test_server_errors_are_judged_by_duration(self, tracker: EmbeddingHealthTracker, clock: Clock) -> None:
        for at in (0, 30, 61, 120):
            fail(tracker, clock, at, status_error(502))
        assert tracker.snapshot("emb").state == "unstable"
        fail(tracker, clock, SLOW_FAILURE_SECONDS, status_error(502))
        assert tracker.snapshot("emb").state == "failing"

    async def test_the_latest_failure_kind_decides(self, tracker: EmbeddingHealthTracker, clock: Clock) -> None:
        """先是网络不通、后来上游开始回 401：按 401 那一档判"""
        fail(tracker, clock, 0, httpx.ConnectError("refused", request=REQUEST))
        fail(tracker, clock, 30, httpx.ConnectError("refused", request=REQUEST))
        fail(tracker, clock, 61, status_error(401))
        assert tracker.snapshot("emb").state == "failing"

    async def test_one_success_resets_the_streak(self, tracker: EmbeddingHealthTracker, clock: Clock) -> None:
        fail(tracker, clock, 0, status_error(401))
        fail(tracker, clock, 30, status_error(401))
        clock.now += 1
        tracker.record_success("emb")
        fail(tracker, clock, 61, status_error(401))
        health = tracker.snapshot("emb")
        assert health.state == "unstable" and health.consecutive_failures == 1

    async def test_groups_are_tracked_separately(self, tracker: EmbeddingHealthTracker, clock: Clock) -> None:
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401), group="memory-emb")
        fail(tracker, clock, 62, status_error(401), group="kb-emb")
        assert tracker.snapshot("memory-emb").state == "failing"
        assert tracker.snapshot("kb-emb").state == "unstable"

    async def test_input_rejections_do_not_count_as_failures(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
        logged: List[Tuple[str, str]],
    ) -> None:
        """一个小时里每分钟一条太长、被审核拦下的输入：只记次数，不判长期失败、不提醒"""
        for at in range(0, 3600, 60):
            fail(tracker, clock, at, status_error(400))
        fail(tracker, clock, 3600, status_error(413))
        fail(tracker, clock, 3601, status_error(422))
        await tracker.drain_alerts()

        health = tracker.snapshot("emb")
        assert health is not None
        assert (health.state, health.consecutive_failures, health.kind) == ("ok", 0, "")
        assert (health.input_rejections, health.last_input_status) == (62, 422)
        assert health.last_input_rejection_at == clock()
        assert sent.calls == []
        assert not levels(logged, "ERROR", "嵌入调用长期失败")

    async def test_input_rejections_do_not_break_a_real_streak(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
    ) -> None:
        """Key 失效的那一串里夹着一条被拒的输入：照样判，次数只算真正的失败"""
        fail(tracker, clock, 0, status_error(401))
        fail(tracker, clock, 20, status_error(400))
        fail(tracker, clock, 30, status_error(401))
        fail(tracker, clock, 61, status_error(401))
        health = tracker.snapshot("emb")
        assert health is not None
        assert (health.state, health.kind, health.consecutive_failures, health.input_rejections) == (
            "failing",
            "auth",
            3,
            1,
        )

    async def test_a_wall_clock_jumping_forward_does_not_shorten_the_wait(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
    ) -> None:
        """系统时间往前校准了一个小时：「半小时没成功过一次」照真实流逝的时间算，不提前判"""
        error = httpx.ConnectError("refused", request=REQUEST)
        fail(tracker, clock, 0, error)
        clock.wall_offset = 3600.0
        fail(tracker, clock, 60, error)
        fail(tracker, clock, 120, error)
        assert tracker.snapshot("emb").state == "unstable"

    async def test_a_wall_clock_set_back_does_not_block_the_judgement(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
    ) -> None:
        """系统时间往回拨了两个小时：跨度仍是真实的一分钟多，照样判；展示的时间用墙上时间"""
        fail(tracker, clock, 0, status_error(401))
        clock.wall_offset = -7200.0
        fail(tracker, clock, 30, status_error(401))
        fail(tracker, clock, 61, status_error(401))
        health = tracker.snapshot("emb")
        assert health is not None and health.state == "failing"
        assert health.last_failure_at == clock() and health.failing_since == clock()

    def test_empty_group_name_is_ignored(self, tracker: EmbeddingHealthTracker) -> None:
        tracker.record_failure("", status_error(401))
        tracker.record_success("")
        assert tracker.snapshots() == []

    def test_old_groups_are_dropped_when_full(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(embedding_health, "MAX_TRACKED_GROUPS", 2)
        fail(tracker, clock, 0, status_error(401), group="a")
        fail(tracker, clock, 1, status_error(401), group="b")
        fail(tracker, clock, 2, status_error(401), group="c")
        assert [item.model_group for item in tracker.snapshots()] == ["b", "c"]

    async def test_a_failing_group_is_kept_when_full(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """满了先丢不在告警的组：正在告警的那个最久没动也留着，不会被丢掉以后再提醒一次"""
        monkeypatch.setattr(embedding_health, "MAX_TRACKED_GROUPS", 2)
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401), group="a")
        fail(tracker, clock, 100, status_error(401), group="b")
        fail(tracker, clock, 200, status_error(401), group="c")
        assert [item.model_group for item in tracker.snapshots()] == ["a", "c"]
        assert tracker.is_failing("a")

        fail(tracker, clock, 300, status_error(401), group="a")
        await tracker.drain_alerts()
        assert len(sent.calls) == 1

    async def test_a_recently_alerted_group_is_kept_over_a_quiet_one(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """刚提醒过、已经恢复的组也晚点丢：丢了它，上次提醒的时间跟着没了，抖动防护就失效"""
        monkeypatch.setattr(embedding_health, "MAX_TRACKED_GROUPS", 2)
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401), group="a")
        clock.now += 1
        tracker.record_success("a")
        fail(tracker, clock, 100, status_error(401), group="b")
        fail(tracker, clock, 200, status_error(401), group="c")
        assert [item.model_group for item in tracker.snapshots()] == ["a", "c"]

        for at in (300, 330, 361):
            fail(tracker, clock, at, status_error(401), group="a")
        await tracker.drain_alerts()
        assert len(sent.calls) == 1, "还在提醒间隔里：又坏了也不再发"


class TestNoFlooding:
    async def test_one_error_and_one_alert_while_failing(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
        logged: List[Tuple[str, str]],
    ) -> None:
        for at in range(0, 3600, 20):
            fail(tracker, clock, at, status_error(401))
        await tracker.drain_alerts()

        assert len(levels(logged, "ERROR", "嵌入调用长期失败")) == 1
        assert len(sent.calls) == 1
        assert tracker.snapshot("emb").consecutive_failures == 180

    async def test_recovery_logs_one_info(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        logged: List[Tuple[str, str]],
    ) -> None:
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401))
        clock.now += 10
        tracker.record_success("emb")
        tracker.record_success("emb")

        recovered = levels(logged, "INFO", "嵌入调用已恢复")
        assert len(recovered) == 1
        assert "memory_backfill_vectors" in recovered[0], "恢复时提一句怎么补失败期间缺的向量"
        health = tracker.snapshot("emb")
        assert health.state == "ok" and health.consecutive_failures == 0 and health.kind == ""
        assert health.last_success_at == clock.now and health.failing_since is None

    async def test_flapping_does_not_realert_within_the_interval(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
        logged: List[Tuple[str, str]],
    ) -> None:
        def incident(start: float) -> None:
            for offset in (0, 30, 61):
                fail(tracker, clock, start + offset, status_error(401))
            clock.now += 1
            tracker.record_success("emb")

        incident(0)
        incident(3600)
        await tracker.drain_alerts()
        assert len(sent.calls) == 1, "好了又坏，间隔内不再提醒"
        assert len(levels(logged, "ERROR", "嵌入调用长期失败")) == 2, "每次进入都记一条 ERROR"

        incident(REALERT_MIN_SECONDS + 100)
        await tracker.drain_alerts()
        assert len(sent.calls) == 2

    async def test_a_failing_sender_does_not_escape(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        logged: List[Tuple[str, str]],
    ) -> None:
        async def broken(*_args: Any) -> None:
            raise RuntimeError(f"webhook down {SECRET}")

        tracker.alert_sender = broken
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401))
        await tracker.drain_alerts()
        warnings = levels(logged, "WARNING", "嵌入失败提醒投递出错")
        assert len(warnings) == 1 and SECRET not in warnings[0]


class TestNoKey:
    async def test_logs_alert_and_snapshot_carry_no_key(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
        sent: Sent,
        logged: List[Tuple[str, str]],
    ) -> None:
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401))
        clock.now += 5
        tracker.record_success("emb")
        await tracker.drain_alerts()

        assert logged, "前提：这一串里确实记了日志"
        assert not [message for _level, message in logged if SECRET in message]
        (health, _requesters) = sent.calls[0]
        title, body = embedding_health.render_alert(health)
        assert SECRET not in title + body
        assert "鉴权失败" in body and "HTTP 401" in body
        assert SECRET not in repr(health)


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def make_groups() -> None:
    await DBModelGroup.create(name="emb-platform", chat_model="text-emb", model_type="embedding")
    await DBModelGroup.create(
        name="emb-tenant5",
        chat_model="text-emb",
        model_type="embedding",
        ownership="tenant",
        owner_tenant_id=5,
    )
    for adapter_key, instance_key, tenant_id in (
        ("onebot_v11", "t5a", 5),
        ("onebot_v11", "t5b", 5),
        ("onebot_v11", "t6a", 6),
        ("onebot_v11", "p1", None),
    ):
        await DBAdapterInstance.create(adapter_key=adapter_key, instance_key=instance_key, tenant_id=tenant_id)


class Dispatched:
    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []

    async def __call__(self, title: str, body: str, level: str = "info", **kwargs: Any) -> dict[str, bool]:
        self.calls.append({"title": title, "body": body, "level": level, **kwargs})
        return {"webhook": True}


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> Dispatched:
    recorder = Dispatched()
    monkeypatch.setattr(channels, "dispatch_notification", recorder)
    return recorder


def install_webhooks(monkeypatch: pytest.MonkeyPatch, urls: dict[str, str]) -> None:
    """实例层的告警 Webhook：`urls` 里有的实例就是在实例层配过地址的"""
    monkeypatch.setattr(channels, "alert_webhook_is_instance_owned", lambda _adapter, instance: instance in urls)
    monkeypatch.setattr(
        channels,
        "resolve_alert_config",
        lambda _adapter, instance: SimpleNamespace(INSTANCE_ALERT_WEBHOOK_URL=urls.get(instance, "")),
    )


async def failing_health(group: str) -> embedding_health.EmbeddingHealth:
    """走一遍真实的进入：凑够失败、在后台任务里查库定归属；返回交给提醒的那份快照

    这里只要那份快照，真正的投递由用例自己调 `send_alert`。
    """
    clock = Clock()
    item = EmbeddingHealthTracker(clock=clock, monotonic=clock.mono)
    sent = Sent()
    item.alert_sender = sent
    for at in (0, 30, 61):
        clock.now = 1_000_000.0 + at
        item.record_failure(group, status_error(401))
    await item.drain_alerts()
    assert len(sent.calls) == 1
    health = sent.calls[0][0]
    assert health.state == "failing"
    return health


@pytest.fixture
def memory_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """配了记忆嵌入模型组（缺向量的段数只在这时给）"""
    monkeypatch.setattr(embedding_health, "_memory_embedding_group", lambda: "emb-platform")


class TestRouting:
    async def test_a_platform_group_alerts_the_platform_only(
        self,
        db: None,
        dispatched: Dispatched,
        memory_group: None,
    ) -> None:
        await make_groups()

        health = await failing_health("emb-platform")
        assert (health.owner, health.owner_tenant_id) == ("platform", None)
        sent = await embedding_health.send_alert(health, (("onebot_v11", "t5a"),))

        assert sent == [("platform", "", "")]
        assert len(dispatched.calls) == 1
        call = dispatched.calls[0]
        assert call["level"] == "error" and "emb-platform" in call["title"]
        assert not call.get("scoped_only") and not call.get("adapter_key"), "平台方走全局渠道，不按实例"
        assert SECRET not in call["title"] + call["body"]
        assert "目前有 0 段有效的记忆缺向量" in call["body"], "平台方那一份带全平台缺向量的段数"

    async def test_a_group_missing_when_it_starts_failing_alerts_nobody(
        self,
        db: None,
        dispatched: Dispatched,
        logged: List[Tuple[str, str]],
    ) -> None:
        """进入时库里就查不到（组已经删了）：归属不明，宁可不发，也不当成平台方的发到全局渠道"""
        health = await failing_health("deleted-group")
        assert health.owner == "unknown"
        assert await embedding_health.send_alert(health, ()) == []
        assert dispatched.calls == []
        assert levels(logged, "WARNING", "查不到它归谁")

    async def test_a_snapshot_without_an_owner_alerts_nobody(self, dispatched: Dispatched) -> None:
        """归属没查定的快照（查库出错）同样不发：不再现查，更不默认成平台方"""

        async def not_found_yet(_model_group: str) -> Tuple[str, Optional[int]]:
            return "", None

        clock = Clock()
        item = EmbeddingHealthTracker(clock=clock, monotonic=clock.mono)
        item.owner_resolver = not_found_yet
        for at in (0, 30, 61):
            clock.now = 1_000_000.0 + at
            item.record_failure("emb-tenant5", status_error(401))
        await item.drain_alerts()
        health = item.snapshot("emb-tenant5")
        assert health is not None and health.state == "failing" and health.owner == ""
        assert await embedding_health.send_alert(health, ()) == []
        assert dispatched.calls == []

    async def test_a_failing_owner_lookup_sends_nothing(
        self,
        dispatched: Dispatched,
        logged: List[Tuple[str, str]],
    ) -> None:
        async def broken(_model_group: str) -> Tuple[str, Optional[int]]:
            raise RuntimeError("数据库不可用")

        clock = Clock()
        item = EmbeddingHealthTracker(clock=clock, monotonic=clock.mono)
        item.owner_resolver = broken
        for at in (0, 30, 61):
            clock.now = 1_000_000.0 + at
            item.record_failure("emb-tenant5", status_error(401))
        await item.drain_alerts()

        assert dispatched.calls == []
        assert levels(logged, "WARNING", "归属出错")
        assert item.snapshot("emb-tenant5").owner == ""

    async def test_the_owner_lives_only_as_long_as_the_failure(
        self,
        tracker: EmbeddingHealthTracker,
        clock: Clock,
    ) -> None:
        """查回来的归属记在这一次长期失败上，恢复就清掉；查的工夫里已经恢复了，查回来的也不记"""
        for at in (0, 30, 61):
            fail(tracker, clock, at, status_error(401))
        await tracker.drain_alerts()
        assert tracker.snapshot("emb").owner == "platform"

        clock.now += 1
        tracker.record_success("emb")
        assert tracker.snapshot("emb").owner == ""

        for at in (100, 130, 161):
            fail(tracker, clock, at, status_error(401))
        assert tracker.is_failing("emb")
        clock.now += 1
        tracker.record_success("emb")
        await tracker.drain_alerts()
        assert tracker.snapshot("emb").owner == ""

    async def test_a_tenant_group_alerts_only_that_tenants_own_webhooks(
        self,
        db: None,
        dispatched: Dispatched,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """别的租户的实例也在用这个组，但它的 Webhook 不收；平台方的全局渠道也不收"""
        await make_groups()
        install_webhooks(
            monkeypatch,
            {
                "t5a": "https://hook.tenant5.invalid/x",
                "t5b": "https://hook.tenant5.invalid/x",
                "t6a": "https://hook.t6.invalid",
            },
        )

        health = await failing_health("emb-tenant5")
        assert (health.owner, health.owner_tenant_id) == ("tenant", 5)
        sent = await embedding_health.send_alert(
            health,
            (("onebot_v11", "t6a"), ("onebot_v11", "t5b"), ("onebot_v11", "p1")),
        )

        assert sent == [("tenant", "onebot_v11", "t5b")], "最近用过这个组的、本租户的实例在前；同一个地址只发一次"
        assert len(dispatched.calls) == 1
        call = dispatched.calls[0]
        assert call["scoped_only"] is True and (call["adapter_key"], call["instance_key"]) == ("onebot_v11", "t5b")
        assert "段有效的记忆缺向量" not in call["body"], "全平台缺向量的段数不发给租户"

    async def test_a_tenant_group_without_requesters_uses_the_tenants_instances(
        self,
        db: None,
        dispatched: Dispatched,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """知识库那几处调用不带实例：按这个租户名下的实例找它自己配的 Webhook，各地址各发一次"""
        await make_groups()
        install_webhooks(monkeypatch, {"t5a": "https://hook-a.invalid", "t5b": "https://hook-b.invalid"})

        sent = await embedding_health.send_alert(await failing_health("emb-tenant5"), ())

        assert sent == [("tenant", "onebot_v11", "t5a"), ("tenant", "onebot_v11", "t5b")]
        assert all(call["scoped_only"] is True for call in dispatched.calls)

    async def test_a_tenant_without_its_own_webhook_gets_nothing_and_the_platform_neither(
        self,
        db: None,
        dispatched: Dispatched,
        monkeypatch: pytest.MonkeyPatch,
        logged: List[Tuple[str, str]],
    ) -> None:
        await make_groups()
        install_webhooks(monkeypatch, {})

        health = await failing_health("emb-tenant5")
        assert await embedding_health.send_alert(health, (("onebot_v11", "t5a"),)) == []
        assert dispatched.calls == []
        assert levels(logged, "WARNING", "没有配告警 Webhook")

    async def test_deleting_the_group_after_it_started_failing_keeps_it_the_tenants(
        self,
        db: None,
        dispatched: Dispatched,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """租户凑够失败以后马上删组：提醒与后台列表仍按进入时查定的归属，带租户组名的提醒不落到平台方"""
        await make_groups()
        install_webhooks(monkeypatch, {"t5a": "https://hook-a.invalid"})
        clock = Clock()
        item = EmbeddingHealthTracker(clock=clock, monotonic=clock.mono)  # 默认的查归属与发提醒
        monkeypatch.setattr(embedding_health, "tracker", item)
        for at in (0, 30, 61):
            clock.now = 1_000_000.0 + at
            item.record_failure("emb-tenant5", status_error(401), adapter_key="onebot_v11", instance_key="t5a")
        await item.drain_alerts()
        assert [call["instance_key"] for call in dispatched.calls] == ["t5a"]

        await DBModelGroup.filter(name="emb-tenant5").delete()

        assert await embedding_health.send_alert(item.snapshot("emb-tenant5"), ()) == [("tenant", "onebot_v11", "t5a")]
        assert all(call.get("scoped_only") is True for call in dispatched.calls), "一条都没走平台方的全局渠道"
        tenant_view = await health_router.get_embedding_health(_current_user=tenant_admin(5))
        assert [entry.model_group for entry in tenant_view.items] == ["emb-tenant5"]
        platform_view = await health_router.get_embedding_health(_current_user=platform_user())
        assert [(entry.model_group, entry.owner) for entry in platform_view.items] == [("emb-tenant5", "tenant")]

    async def test_a_name_reused_after_the_failure_started_does_not_hand_it_to_the_new_owner(
        self,
        db: None,
        dispatched: Dispatched,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """进入时查不到的组，之后别的租户用同一个名字建了组：这一次的失败仍按归属不明，只给平台方看"""
        clock = Clock()
        item = EmbeddingHealthTracker(clock=clock, monotonic=clock.mono)
        monkeypatch.setattr(embedding_health, "tracker", item)
        for at in (0, 30, 61):
            clock.now = 1_000_000.0 + at
            item.record_failure("emb-reused", status_error(401))
        await item.drain_alerts()
        assert dispatched.calls == []

        await DBModelGroup.create(
            name="emb-reused",
            chat_model="text-emb",
            model_type="embedding",
            ownership="tenant",
            owner_tenant_id=6,
        )

        tenant_view = await health_router.get_embedding_health(_current_user=tenant_admin(6))
        assert tenant_view.items == []
        platform_view = await health_router.get_embedding_health(_current_user=platform_user())
        assert [(entry.model_group, entry.owner) for entry in platform_view.items] == [("emb-reused", "unknown")]


def platform_user() -> DBUser:
    user = DBUser(
        username="platform",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )
    user.id = 1
    return user


def tenant_admin(tenant_id: int) -> DBUser:
    user = DBUser(
        username=f"tenant-{tenant_id}",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    user.id = 100 + tenant_id
    return user


async def add_paragraph(content: str, *, ref: Optional[str] = None, inactive: bool = False) -> None:
    await DBMemParagraph.create(
        workspace_id=1,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        origin_kind=OriginKind.CONSOLIDATION,
        content=content,
        embedding_ref=ref,
        is_inactive=inactive,
    )


async def make_paragraphs() -> None:
    """六段记忆：两段有效且没向量；一段有向量、一段失活了，两段没向量但正文全是空白（补不了）"""
    await add_paragraph("第 0 段")
    await add_paragraph("第 1 段", ref="vec-1")
    await add_paragraph("第 2 段", inactive=True)
    await add_paragraph("第 3 段")
    await add_paragraph("")
    await add_paragraph("  \n\t \n")


class TestMissingVectors:
    async def test_only_active_non_blank_paragraphs_without_a_vector_count(self, db: None, memory_group: None) -> None:
        await make_paragraphs()
        assert await embedding_health.count_missing_vectors() == 2

    async def test_counted_at_most_once_a_minute(
        self,
        db: None,
        memory_group: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = Clock()
        monkeypatch.setattr(embedding_health, "_cache_clock", clock.mono)
        await make_paragraphs()
        assert await embedding_health.count_missing_vectors() == 2

        await add_paragraph("第 4 段")
        clock.now += embedding_health.MISSING_VECTORS_CACHE_SECONDS - 1
        assert await embedding_health.count_missing_vectors() == 2, "一分钟内用缓存的数"
        clock.now += 2
        assert await embedding_health.count_missing_vectors() == 3

    async def test_no_count_without_a_memory_embedding_group(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没配记忆嵌入模型组：补不了，也就不给数（后台不显示蓝条）"""
        monkeypatch.setattr(embedding_health, "_memory_embedding_group", lambda: "")
        await make_paragraphs()
        assert await embedding_health.count_missing_vectors() is None

    async def test_a_failing_count_gives_none_and_is_not_cached(
        self,
        db: None,
        memory_group: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查不了时这个附带的数为 None，不让状态页或提醒失败；下一次照常再查"""
        await make_paragraphs()
        original = DBMemParagraph.filter

        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("数据库不可用")

        monkeypatch.setattr(DBMemParagraph, "filter", broken)
        assert await embedding_health.count_missing_vectors() is None
        monkeypatch.setattr(DBMemParagraph, "filter", original)
        assert await embedding_health.count_missing_vectors() == 2


class TestConsoleStatus:
    @pytest.fixture
    async def tracked(self, db: None, memory_group: None, monkeypatch: pytest.MonkeyPatch) -> Clock:
        await make_groups()
        clock = Clock()
        monkeypatch.setattr(embedding_health.tracker, "_clock", clock)
        monkeypatch.setattr(embedding_health.tracker, "_monotonic", clock.mono)
        for at in (0, 30, 61):
            clock.now = 1_000_000.0 + at
            embedding_health.tracker.record_failure("emb-platform", status_error(401))
            embedding_health.tracker.record_failure("emb-tenant5", status_error(429))
        clock.now += 1
        embedding_health.tracker.record_success("emb-ok")
        await make_paragraphs()
        monkeypatch.setattr(embedding_health, "_uses_of", lambda name: ["memory"] if name == "emb-platform" else [])
        return clock

    async def test_the_platform_sees_every_group(self, tracked: Clock) -> None:
        response = await health_router.get_embedding_health(_current_user=platform_user())

        by_name = {item.model_group: item for item in response.items}
        assert set(by_name) == {"emb-platform", "emb-tenant5", "emb-ok"}
        platform = by_name["emb-platform"]
        assert (platform.owner, platform.state, platform.error_kind, platform.status_code) == (
            "platform",
            "failing",
            "auth",
            401,
        )
        assert platform.uses == ["memory"]
        assert platform.failing_since is not None and platform.last_failure_at is not None
        assert platform.last_success_at is None
        assert by_name["emb-tenant5"].owner == "tenant"
        assert by_name["emb-ok"].state == "ok" and by_name["emb-ok"].last_success_at is not None
        assert SECRET not in response.model_dump_json()
        assert response.missing_vectors == 2

    async def test_a_tenant_sees_only_its_own_groups(self, tracked: Clock) -> None:
        response = await health_router.get_embedding_health(_current_user=tenant_admin(5))
        assert [item.model_group for item in response.items] == ["emb-tenant5"]
        assert response.items[0].error_kind == "quota"
        assert response.missing_vectors is None, "缺向量的段数是全平台的，不给租户"

    async def test_another_tenant_sees_nothing(self, tracked: Clock) -> None:
        response = await health_router.get_embedding_health(_current_user=tenant_admin(6))
        assert response.items == []

    async def test_no_missing_count_without_a_memory_embedding_group(
        self,
        tracked: Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(embedding_health, "_memory_embedding_group", lambda: "")
        response = await health_router.get_embedding_health(_current_user=platform_user())
        assert response.missing_vectors is None

    async def test_a_deleted_group_shows_as_unknown_to_the_platform_only(self, tracked: Clock) -> None:
        """没进入长期失败、组又被删了：平台方看到归属不明，租户谁都看不到"""
        embedding_health.tracker.record_failure("emb-gone", status_error(401))
        platform = await health_router.get_embedding_health(_current_user=platform_user())
        assert {item.model_group: item.owner for item in platform.items}["emb-gone"] == "unknown"
        for tenant_id in (5, 6):
            response = await health_router.get_embedding_health(_current_user=tenant_admin(tenant_id))
            assert "emb-gone" not in [item.model_group for item in response.items]

    async def test_input_rejections_are_shown(self, tracked: Clock) -> None:
        embedding_health.tracker.record_failure("emb-ok", status_error(413))
        response = await health_router.get_embedding_health(_current_user=platform_user())
        item = {entry.model_group: entry for entry in response.items}["emb-ok"]
        assert (item.state, item.input_rejections, item.last_input_status) == ("ok", 1, 413)
        assert item.last_input_rejection_at is not None


class FakeClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        return self.response

    async def aclose(self) -> None:
        return None


class TestRecordedAtTheEmbeddingCall:
    @pytest.fixture(autouse=True)
    def _no_ledger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(openai_module, "record_llm_usage", lambda **_kwargs: None)

    def install(self, monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: FakeClient(response))
        # 连通性测试用一次性客户端（不进按地址缓存的连接池），两条路都换掉，免得真的去解析域名
        monkeypatch.setattr(openai_module, "_create_http_client", lambda **_kwargs: FakeClient(response))

    async def call(self, *, group: str = "emb", requester: Optional[Tuple[str, str]] = None) -> List[float]:
        context = LLMUsageContext(
            scene=LLMUsageScene.EMBEDDING,
            adapter_key=requester[0] if requester else "",
            instance_key=requester[1] if requester else "",
        ).with_model_group(group, None)
        with usage_scope(context):
            return await openai_module.gen_openai_embeddings(
                model="text-emb",
                input="hello",
                api_key=SECRET,
                base_url="https://upstream.invalid/v1",
                tenant_egress=False,
            )

    async def test_failure_and_success_are_recorded_per_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.install(monkeypatch, httpx.Response(401, request=REQUEST, text=f"bad key {SECRET}"))
        with pytest.raises(httpx.HTTPStatusError):
            await self.call(requester=("onebot_v11", "inst1"))
        health = embedding_health.tracker.snapshot("emb")
        assert health is not None
        assert (health.state, health.kind, health.status_code, health.consecutive_failures) == (
            "unstable",
            "auth",
            401,
            1,
        )

        self.install(monkeypatch, httpx.Response(200, request=REQUEST, json={"data": [{"embedding": [0.1, 0.2]}]}))
        assert await self.call() == [0.1, 0.2]
        assert embedding_health.tracker.snapshot("emb").state == "ok"

    async def test_an_input_rejection_is_counted_apart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.install(monkeypatch, httpx.Response(400, request=REQUEST, text=f"input too long {SECRET}"))
        with pytest.raises(httpx.HTTPStatusError):
            await self.call()
        health = embedding_health.tracker.snapshot("emb")
        assert health is not None
        assert (health.state, health.consecutive_failures, health.input_rejections) == ("ok", 0, 1)

    async def test_a_malformed_body_counts_as_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.install(monkeypatch, httpx.Response(200, request=REQUEST, json={"data": []}))
        with pytest.raises(IndexError):
            await self.call()
        assert embedding_health.tracker.snapshot("emb").kind == "server"

    async def test_untracked_calls_are_not_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模型组连通性测试测的可能是没保存的草稿：成败都不计入"""
        self.install(monkeypatch, httpx.Response(401, request=REQUEST))
        with embedding_health.untracked(), pytest.raises(httpx.HTTPStatusError):
            await self.call()
        assert embedding_health.tracker.snapshot("emb") is None

    async def test_calls_without_a_model_group_are_not_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.install(monkeypatch, httpx.Response(401, request=REQUEST))
        with pytest.raises(httpx.HTTPStatusError):
            await self.call(group="")
        assert embedding_health.tracker.snapshots() == []

    async def test_the_model_group_test_does_not_touch_the_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import ModelConfigGroup
        from nekro_agent.services import model_group_test

        self.install(monkeypatch, httpx.Response(401, request=REQUEST))
        group = ModelConfigGroup(
            CHAT_MODEL="text-emb",
            MODEL_TYPE="embedding",
            BASE_URL="https://upstream.invalid/v1",
            API_KEY=SECRET,
        )

        outcome = await model_group_test.run_model_group_test(
            group,
            credential_visible=True,
            group_name="emb",
            tenant_egress=False,
            tenant_id=None,
        )

        assert outcome.ok is False
        assert outcome.error_code is model_group_test.ModelTestErrorCode.UNAUTHORIZED, "前提：确实打到了上游并拿到 401"
        assert embedding_health.tracker.snapshot("emb") is None
