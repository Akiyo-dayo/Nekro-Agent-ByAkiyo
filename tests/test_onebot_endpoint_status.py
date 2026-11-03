"""OneBot 协议端识别与「上报自身消息」自检

判据、内存状态与三个挂钩点（发送计数、回传计数、连接时查版本）都在这里钉住。
用量级的事件形态与测试替身复用 `test_onebot_video_visibility.py`：`message_sent`
走真实匹配器，这样「被去重认成 NA 自己发的那条也要计数」才是真的验到了。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nonebot.adapters.onebot.v11 import Bot
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import endpoint_status as endpoint_status_module
from nekro_agent.adapters.onebot_v11.core import self_sent as self_sent_module
from nekro_agent.adapters.onebot_v11.core.endpoint_status import (
    EndpointInfo,
    EndpointStatusTracker,
    SelfReportState,
)
from nekro_agent.adapters.onebot_v11.core.instance import clear_instance_cache
from nekro_agent.adapters.onebot_v11.core.self_sent import SelfSentRegistry
from nekro_agent.adapters.onebot_v11.matchers import message as matcher_module
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.routers.console import onebot_endpoint as onebot_endpoint_module
from nekro_agent.services.user.deps import get_current_active_user
from tests.test_onebot_video_visibility import (  # noqa: F401 - fixtures are used by name
    BOT_ID,
    FakeProtocol,
    pipeline,
    self_event,
    text_segment,
)
from tests.test_qbob_console import ACME_TENANT_ID, RIVAL_TENANT_ID, acme_admin
from tests.test_video_analysis_plugin import harness  # noqa: F401 - `pipeline` depends on it

SELF_ID = "10001"
SELF_SENT_APIS = ("send_group_msg", "send_msg", "send_private_msg")


class FakeClock:
    """单调时钟与墙上时钟分开推进：`connected_at` 用后者，宽限期用前者"""

    def __init__(self) -> None:
        self.monotonic = 0.0
        self.wall = 1_700_000_000.0

    def clock(self) -> float:
        return self.monotonic

    def wall_clock(self) -> float:
        return self.wall


class CaptureLogger:
    """只关心调用方传进来的文案，不需要 loguru 的整条管道"""

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.infos: List[str] = []
        self.debugs: List[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(str(message))

    def info(self, message: str) -> None:
        self.infos.append(str(message))

    def debug(self, message: str) -> None:
        self.debugs.append(str(message))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker(clock: FakeClock) -> EndpointStatusTracker:
    return EndpointStatusTracker(clock=clock.clock, wall_clock=clock.wall_clock)


@pytest.fixture(autouse=True)
def _fresh_instance_cache() -> Any:
    clear_instance_cache()
    yield
    clear_instance_cache()


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class TestTracker:
    def test_an_unseen_account_is_empty_and_unknown(self, tracker: EndpointStatusTracker) -> None:
        snapshot = tracker.snapshot("19999")

        assert snapshot.info is None
        assert snapshot.implementation == "unknown"
        assert snapshot.self_report is SelfReportState.UNKNOWN
        assert (snapshot.own_sends, snapshot.echoes) == (0, 0)
        assert snapshot.connected_at is None
        assert snapshot.last_echo_at is None

    def test_three_sends_inside_the_grace_period_are_still_unknown(
        self, tracker: EndpointStatusTracker, clock: FakeClock
    ) -> None:
        tracker.reset(SELF_ID)
        for at in (0.0, 20.0, 40.0):
            clock.monotonic = at
            tracker.note_own_send(SELF_ID)

        clock.monotonic = 59.0
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.UNKNOWN

    def test_grace_counting_starts_at_the_first_counted_send(
        self, tracker: EndpointStatusTracker, clock: FakeClock
    ) -> None:
        tracker.reset(SELF_ID)
        for at in (0.0, 20.0, 40.0):
            clock.monotonic = at
            tracker.note_own_send(SELF_ID)

        clock.monotonic = 59.9
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.UNKNOWN
        clock.monotonic = 60.0
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.OFF_SUSPECTED

    def test_two_sends_never_mark_it_off(self, tracker: EndpointStatusTracker, clock: FakeClock) -> None:
        tracker.reset(SELF_ID)
        tracker.note_own_send(SELF_ID)
        tracker.note_own_send(SELF_ID)
        clock.monotonic = 600.0

        snapshot = tracker.snapshot(SELF_ID)
        assert snapshot.own_sends == 2
        assert snapshot.self_report is SelfReportState.UNKNOWN

    def test_an_echo_turns_it_on(self, tracker: EndpointStatusTracker, clock: FakeClock) -> None:
        tracker.reset(SELF_ID)
        tracker.note_echo(SELF_ID)
        clock.monotonic = 600.0
        tracker.note_own_send(SELF_ID)
        tracker.note_own_send(SELF_ID)

        snapshot = tracker.snapshot(SELF_ID)
        assert snapshot.echoes == 1
        assert snapshot.last_echo_at is not None
        assert snapshot.self_report is SelfReportState.ON

    def test_each_send_followed_by_its_echo_stays_on(self, tracker: EndpointStatusTracker, clock: FakeClock) -> None:
        tracker.reset(SELF_ID)
        for at in range(0, 600, 30):
            clock.monotonic = float(at)
            tracker.note_own_send(SELF_ID)
            tracker.note_echo(SELF_ID)

        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.ON

    def test_turning_it_off_without_reconnecting_is_noticed(
        self,
        tracker: EndpointStatusTracker,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SnowLuma 改开关是热重载、不断线：开过之后被关掉，也要从「最近没有回传」认出来"""
        capture = CaptureLogger()
        monkeypatch.setattr(endpoint_status_module, "logger", capture)
        tracker.reset(SELF_ID)
        tracker.note_own_send(SELF_ID)
        tracker.note_echo(SELF_ID)
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.ON

        for at in (100.0, 110.0, 120.0):
            clock.monotonic = at
            tracker.note_own_send(SELF_ID)
        clock.monotonic = 159.0
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.ON
        clock.monotonic = 160.0
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.OFF_SUSPECTED
        assert len(capture.warnings) == 1

        # 再打开：下一条回传立刻恢复，之后再关掉还能再提醒一次
        tracker.note_echo(SELF_ID)
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.ON
        for at in (300.0, 310.0, 320.0):
            clock.monotonic = at
            tracker.note_own_send(SELF_ID)
        clock.monotonic = 400.0
        assert tracker.snapshot(SELF_ID).self_report is SelfReportState.OFF_SUSPECTED
        assert len(capture.warnings) == 2

    def test_reset_clears_counts_and_info(self, tracker: EndpointStatusTracker, clock: FakeClock) -> None:
        tracker.reset(SELF_ID)
        tracker.note_info(SELF_ID, EndpointInfo("SnowLuma", "1.2.3-node", "11"))
        tracker.note_own_send(SELF_ID)
        tracker.note_echo(SELF_ID)
        clock.monotonic = 100.0

        tracker.reset(SELF_ID)
        snapshot = tracker.snapshot(SELF_ID)

        assert snapshot.info is None
        assert (snapshot.own_sends, snapshot.echoes) == (0, 0)
        assert snapshot.self_report is SelfReportState.UNKNOWN
        assert snapshot.connected_at == clock.wall
        assert snapshot.last_echo_at is None

    def test_the_warning_fires_once_per_connection(
        self,
        tracker: EndpointStatusTracker,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture = CaptureLogger()
        monkeypatch.setattr(endpoint_status_module, "logger", capture)

        tracker.reset(SELF_ID)
        tracker.note_info(SELF_ID, EndpointInfo("SnowLuma", "1.2.3-node", "11"))
        tracker.note_own_send(SELF_ID)
        tracker.note_own_send(SELF_ID)
        clock.monotonic = 60.0
        tracker.note_own_send(SELF_ID)  # 第三条发出时宽限期已过：这条负责告警
        tracker.note_own_send(SELF_ID)  # 之后再发不重复告警
        tracker.snapshot(SELF_ID)

        assert len(capture.warnings) == 1
        assert "SnowLuma 1.2.3-node" in capture.warnings[0]
        assert "上报自身消息" in capture.warnings[0]

        tracker.reset(SELF_ID)
        for _ in range(3):
            tracker.note_own_send(SELF_ID)
        clock.monotonic = 200.0
        tracker.snapshot(SELF_ID)
        assert len(capture.warnings) == 2

    @pytest.mark.parametrize(
        ("app_name", "expected"),
        [
            ("SnowLuma", "snowluma"),
            ("snowluma", "snowluma"),
            ("NapCat.Onebot", "napcat"),
            ("go-cqhttp", "other"),
            ("", "unknown"),
        ],
    )
    def test_implementation_classification(
        self, tracker: EndpointStatusTracker, app_name: str, expected: str
    ) -> None:
        tracker.reset(SELF_ID)
        tracker.note_info(SELF_ID, EndpointInfo(app_name=app_name))

        assert tracker.snapshot(SELF_ID).implementation == expected

    def test_missing_info_is_unknown(self, tracker: EndpointStatusTracker) -> None:
        tracker.reset(SELF_ID)
        tracker.note_info(SELF_ID, None)

        assert tracker.snapshot(SELF_ID).implementation == "unknown"


# ---------------------------------------------------------------------------
# 挂钩点：发送计数
# ---------------------------------------------------------------------------


async def _call_called_api(
    api: str,
    result: Any,
    *,
    data: Optional[Dict[str, Any]] = None,
    exception: Optional[Exception] = None,
) -> None:
    bot = Bot(FakeProtocol(), SELF_ID)  # type: ignore[arg-type]
    await self_sent_module._on_called_api(bot, exception, api, data or {"group_id": 1001}, result)


@pytest.fixture
def endpoint_tracker(monkeypatch: pytest.MonkeyPatch) -> EndpointStatusTracker:
    tracker = EndpointStatusTracker()
    monkeypatch.setattr(self_sent_module, "endpoint_status", tracker)
    monkeypatch.setattr(self_sent_module, "self_sent_registry", SelfSentRegistry())
    monkeypatch.setattr(matcher_module, "endpoint_status", tracker)
    return tracker


class TestOwnSendHook:
    async def test_a_successful_group_send_with_a_negative_id_counts(
        self, endpoint_tracker: EndpointStatusTracker
    ) -> None:
        await _call_called_api("send_group_msg", {"message_id": -12345678})

        assert endpoint_tracker.snapshot(SELF_ID).own_sends == 1

    @pytest.mark.parametrize("api", SELF_SENT_APIS)
    async def test_all_three_send_actions_count(
        self, endpoint_tracker: EndpointStatusTracker, api: str
    ) -> None:
        await _call_called_api(api, {"message_id": 12345}, data={"group_id": 1001, "user_id": 10002})

        assert endpoint_tracker.snapshot(SELF_ID).own_sends == 1

    async def test_a_zero_message_id_does_not_count(self, endpoint_tracker: EndpointStatusTracker) -> None:
        await _call_called_api("send_group_msg", {"message_id": 0})

        assert endpoint_tracker.snapshot(SELF_ID).own_sends == 0

    async def test_a_failed_send_does_not_count(self, endpoint_tracker: EndpointStatusTracker) -> None:
        await _call_called_api("send_group_msg", {"message_id": 12345}, exception=RuntimeError("发送失败"))

        assert endpoint_tracker.snapshot(SELF_ID).own_sends == 0

    @pytest.mark.parametrize(
        ("api", "data", "result"),
        [
            ("upload_group_file", {"group_id": 1001, "file": "a.zip", "name": "a.zip"}, {"file_id": "a"}),
            ("send_group_forward_msg", {"group_id": 1001}, {"message_id": 12345}),
            ("send_group_ai_record", {"group_id": 1001}, {"message_id": 0}),
        ],
    )
    async def test_other_send_forms_are_not_counted(
        self,
        endpoint_tracker: EndpointStatusTracker,
        api: str,
        data: Dict[str, Any],
        result: Any,
    ) -> None:
        await _call_called_api(api, result, data=data)

        assert endpoint_tracker.snapshot(SELF_ID).own_sends == 0


# ---------------------------------------------------------------------------
# 挂钩点：回传计数（走真实匹配器）
# ---------------------------------------------------------------------------


class TestEchoHook:
    async def test_an_echo_from_another_program_counts(self, db: None, pipeline: Any, endpoint_tracker: EndpointStatusTracker) -> None:  # noqa: F811
        await pipeline.feed(self_event([text_segment("Yunzai 发的")], message_id=7001))

        snapshot = endpoint_tracker.snapshot(BOT_ID)
        assert snapshot.echoes == 1
        assert snapshot.self_report is SelfReportState.ON

    async def test_an_echo_of_our_own_message_counts_before_deduplication(self, db: None, pipeline: Any, endpoint_tracker: EndpointStatusTracker) -> None:  # noqa: F811
        sent = await pipeline.send_group()
        assert endpoint_tracker.snapshot(BOT_ID).own_sends == 1

        # 这条回传会被去重表认出是 NA 自己发的并丢掉，但它恰恰是「开着」的证据
        await pipeline.feed(self_event([text_segment("NA 的回复")], message_id=int(sent["message_id"])))

        snapshot = endpoint_tracker.snapshot(BOT_ID)
        assert snapshot.echoes == 1
        assert snapshot.self_report is SelfReportState.ON


# ---------------------------------------------------------------------------
# 挂钩点：连接时查版本
# ---------------------------------------------------------------------------


class VersionBot:
    """`on_onebot_connect` 只碰到 `self_id` 与 `call_api`"""

    def __init__(self, self_id: str, reply: Any) -> None:
        self.self_id = self_id
        self.reply = reply
        self.calls: List[str] = []

    async def call_api(self, api: str) -> Any:
        self.calls.append(api)
        if isinstance(self.reply, Exception):
            raise self.reply
        if callable(self.reply):
            return await self.reply()
        return self.reply


async def _drain_probes() -> None:
    for _ in range(100):
        tasks = list(endpoint_status_module._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        if not endpoint_status_module._background_tasks:
            return


class TestOnConnect:
    async def test_reset_then_record_the_endpoint_identity(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        tracker = EndpointStatusTracker(clock=clock.clock, wall_clock=clock.wall_clock)
        capture = CaptureLogger()
        monkeypatch.setattr(endpoint_status_module, "endpoint_status", tracker)
        monkeypatch.setattr(endpoint_status_module, "logger", capture)
        tracker.note_info(SELF_ID, EndpointInfo("Old", "0.0.1", "11"))
        tracker.note_own_send(SELF_ID)
        tracker.note_echo(SELF_ID)

        bot = VersionBot(
            SELF_ID,
            {"app_name": "SnowLuma", "app_version": "1.2.3-node", "protocol_version": "11"},
        )
        await endpoint_status_module.on_onebot_connect(bot)  # type: ignore[arg-type]
        await _drain_probes()

        snapshot = tracker.snapshot(SELF_ID)
        assert (snapshot.own_sends, snapshot.echoes) == (0, 0)
        assert snapshot.connected_at == clock.wall
        assert snapshot.info == EndpointInfo("SnowLuma", "1.2.3-node", "11")
        assert snapshot.implementation == "snowluma"
        assert bot.calls == ["get_version_info"]
        assert any("SnowLuma 1.2.3-node" in message for message in capture.infos)

    @pytest.mark.parametrize(
        "reply",
        [RuntimeError("协议端没回话"), ["not", "a", "dict"]],
        ids=["raises", "non-dict"],
    )
    async def test_a_failed_probe_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, reply: Any
    ) -> None:
        tracker = EndpointStatusTracker(clock=clock.clock, wall_clock=clock.wall_clock)
        monkeypatch.setattr(endpoint_status_module, "endpoint_status", tracker)
        bot = VersionBot(SELF_ID, reply)

        await endpoint_status_module.on_onebot_connect(bot)  # type: ignore[arg-type]
        await _drain_probes()

        snapshot = tracker.snapshot(SELF_ID)
        assert snapshot.info is None
        assert snapshot.connected_at == clock.wall
        assert snapshot.implementation == "unknown"

    async def test_a_timeout_stays_silent(self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
        tracker = EndpointStatusTracker(clock=clock.clock, wall_clock=clock.wall_clock)
        monkeypatch.setattr(endpoint_status_module, "endpoint_status", tracker)
        monkeypatch.setattr(endpoint_status_module, "VERSION_QUERY_TIMEOUT_SECONDS", 0.01)

        async def slow() -> Dict[str, str]:
            await asyncio.sleep(0.05)
            return {"app_name": "SnowLuma", "app_version": "1.2.3-node", "protocol_version": "11"}

        await endpoint_status_module.on_onebot_connect(VersionBot(SELF_ID, slow))  # type: ignore[arg-type]
        await _drain_probes()

        assert tracker.snapshot(SELF_ID).info is None


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------


@pytest.fixture
async def db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _login_as(app: FastAPI, user: Any) -> None:
    if user is None:
        app.dependency_overrides.pop(get_current_active_user, None)
        return
    app.dependency_overrides[get_current_active_user] = lambda: user


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(onebot_endpoint_module.router, prefix="/api/console")
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.login_as = lambda user: _login_as(app, user)  # type: ignore[attr-defined]
    return test_client


async def _make_instance(
    instance_key: str,
    *,
    tenant_id: Optional[int] = None,
    adapter_key: str = "onebot_v11",
    account: str = SELF_ID,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="native",
        provider_account_id=account,
        tenant_id=tenant_id,
        metadata_json="{}",
    )


def _connect_bot(monkeypatch: pytest.MonkeyPatch, *, app_name: str = "SnowLuma") -> FakeProtocol:
    protocol = FakeProtocol()
    protocol.replies["get_version_info"] = {
        "app_name": app_name,
        "app_version": "1.2.3-node",
        "protocol_version": "11",
    }
    bot = Bot(protocol, SELF_ID)  # type: ignore[arg-type]
    monkeypatch.setattr(onebot_endpoint_module, "get_bots", lambda: {SELF_ID: bot})
    return protocol


ENDPOINT_PATH = "/api/console/instances/acme1/onebot/endpoint"


class TestEndpointRoute:
    async def test_online_snowluma_reports_identity_and_counts(
        self, db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracker = EndpointStatusTracker()
        tracker.note_own_send(SELF_ID)
        tracker.note_echo(SELF_ID)
        monkeypatch.setattr(onebot_endpoint_module, "endpoint_status", tracker)
        protocol = _connect_bot(monkeypatch)
        await _make_instance("acme1", tenant_id=ACME_TENANT_ID)

        client.login_as(acme_admin())  # type: ignore[attr-defined]
        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["online"] is True
        assert payload["account_id"] == SELF_ID
        assert payload["implementation"] == "snowluma"
        assert payload["app_name"] == "SnowLuma"
        assert payload["app_version"] == "1.2.3-node"
        assert payload["own_sends"] == 1
        assert payload["echoes"] == 1
        assert payload["self_report"] == "on"
        assert [api for api, _ in protocol.calls] == ["get_version_info"]
        # 现场查到的版本要留在快照里，下次打开不必再问一次协议端
        assert tracker.snapshot(SELF_ID).info is not None

    async def test_offline_account_does_not_call_the_endpoint(
        self, db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        protocol = FakeProtocol()
        protocol.replies["get_version_info"] = {"app_name": "SnowLuma", "app_version": "1.2.3-node", "protocol_version": "11"}
        # 在线的只有另一个账号：本实例的账号不在 `get_bots()` 里，不能拿别人的连接来问
        other_bot = Bot(protocol, "10002")  # type: ignore[arg-type]
        monkeypatch.setattr(onebot_endpoint_module, "get_bots", lambda: {"10002": other_bot})
        await _make_instance("acme1", tenant_id=ACME_TENANT_ID, account=SELF_ID)

        client.login_as(acme_admin())  # type: ignore[attr-defined]
        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["online"] is False
        assert payload["account_id"] == SELF_ID
        assert protocol.calls == []

    async def test_an_instance_without_an_account_is_offline(
        self, db: None, client: TestClient
    ) -> None:
        await _make_instance("acme1", tenant_id=ACME_TENANT_ID, account="")

        client.login_as(acme_admin())  # type: ignore[attr-defined]
        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["online"] is False
        assert payload["account_id"] == ""

    async def test_another_adapter_is_404(self, db: None, client: TestClient) -> None:
        await _make_instance("acme1", tenant_id=ACME_TENANT_ID, adapter_key="minecraft")

        client.login_as(acme_admin())  # type: ignore[attr-defined]
        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 404, response.text

    async def test_another_tenants_instance_is_404(self, db: None, client: TestClient) -> None:
        await _make_instance("acme1", tenant_id=RIVAL_TENANT_ID)

        client.login_as(acme_admin())  # type: ignore[attr-defined]
        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 404, response.text

    async def test_own_instance_is_200(self, db: None, client: TestClient) -> None:
        await _make_instance("acme1", tenant_id=ACME_TENANT_ID, account="")

        client.login_as(acme_admin())  # type: ignore[attr-defined]
        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 200, response.text

    async def test_unauthenticated_is_401(self, db: None, client: TestClient) -> None:
        await _make_instance("acme1", tenant_id=ACME_TENANT_ID, account="")

        response = client.get(ENDPOINT_PATH)

        assert response.status_code == 401, response.text
