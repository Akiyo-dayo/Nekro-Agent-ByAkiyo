"""未认领实例只有凭当前未曝光的部署级令牌连上来才分发

修复前：部署级 `ONEBOT_ACCESS_TOKEN` 为空时握手不校验，任何人带一个陌生账号连上来，第一条事件就把它自动登记成
未认领实例，消息、通知、命令照常进 collector、落库、触发 Agent——匿名者能白用平台的模型额度、往会话里塞消息。

规则：已认领的实例不受影响；未认领实例的入站事件只有在这条连接的握手凭的是当前非空、且没对租户可见过的部署级令牌时
才分发（复用超级用户来源判定的握手上下文与令牌指纹集合）。握手阶段不拒绝连接，认领之后同一条连接照常分发。
升级时有聊天记录的存量未认领实例自动认领一次。
"""

import asyncio
import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, MutableMapping, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.interface.collector import collect_message
from nekro_agent.adapters.interface.schemas.platform import PlatformChannel, PlatformMessage, PlatformUser
from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.adapters.onebot_v11.core.access_token import (
    HandshakeRecord,
    OneBotAccessTokenMiddleware,
    TokenSource,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services import unclaimed_dispatch
from nekro_agent.services.unclaimed_dispatch import (
    auto_claim_unclaimed_with_history,
    is_inbound_dispatch_allowed,
    unclaimed_pause_reason,
)
from nekro_agent.services.user import token_exposure
from nekro_agent.services.user.token_exposure import exposure_file, record_exposed_tokens, token_fingerprint

ONEBOT = "onebot_v11"
DEPLOYMENT_TOKEN = "deployment-token"
TENANT_ID = 7
ANON = "anon"
ANON_ACCOUNT = "30001"
PLATFORM = "platform"
CHANNEL_ID = "group_1001"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core.os_env import OsEnv

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    token_exposure._write([], seeded=True)  # noqa: SLF001
    monkeypatch.setattr(token_exposure, "_rebuild_pending", False)
    monkeypatch.setattr(token_exposure, "_rebuild_lock", None)
    monkeypatch.setattr(token_exposure, "_unwritten_fingerprints", set())
    monkeypatch.setattr(unclaimed_dispatch, "_last_warned", {})
    set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
    gate._presented_by_unbound_account.clear()  # noqa: SLF001


def set_deployment_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """握手中间件与 `container_manager` 在模块级导入了解析函数，改环境变量才能让各处看到同一个值"""
    if token:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", token)
    else:
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=TENANT_ID, name="acme", display_name="acme")
        await add_instance(ANON, status="unclaimed", account=ANON_ACCOUNT)
        await add_instance(PLATFORM, status="online", account="20001", token="own-p")
        await add_instance("tenant_inst", status="online", account="20003", token="own-t", tenant_id=TENANT_ID)
        await add_instance("off", status="disabled", account="20004", token="own-o", enabled=False)
        yield
    finally:
        await Tortoise.close_connections()


async def add_instance(
    instance_key: str,
    *,
    status: str,
    account: str = "",
    token: str = "",
    tenant_id: Optional[int] = None,
    enabled: bool = True,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ONEBOT,
        instance_key=instance_key,
        display_name=f"未认领实例 {account}" if status == "unclaimed" else instance_key,
        status=status,
        enabled=enabled,
        provider="onebot_v11" if status == "unclaimed" else "remote_onebot",
        provider_account_id=account,
        tenant_id=tenant_id,
        metadata_json=json.dumps({"access_token": token}) if token else "",
    )


def handshake(token: str, *, source: TokenSource = TokenSource.DEPLOYMENT, instance_key: str = "") -> HandshakeRecord:
    return HandshakeRecord(
        self_id=ANON_ACCOUNT, source=source, instance_key=instance_key, token_fingerprint=token_fingerprint(token),
    )


@contextmanager
def connection(record: Optional[HandshakeRecord]) -> Iterator[None]:
    reset = gate._current_handshake.set(record)  # noqa: SLF001
    try:
        yield
    finally:
        gate._current_handshake.reset(reset)  # noqa: SLF001


async def allowed(instance_key: str = ANON, record: Optional[HandshakeRecord] = None) -> bool:
    with connection(record):
        return await is_inbound_dispatch_allowed(ONEBOT, instance_key)


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------


class TestGate:
    async def test_unclaimed_on_the_current_deployment_token_is_dispatched(self, db) -> None:
        assert await allowed(record=handshake(DEPLOYMENT_TOKEN)) is True

    async def test_a_deployment_fallback_handshake_also_presented_the_deployment_token(self, db) -> None:
        assert await allowed(record=handshake(DEPLOYMENT_TOKEN, source=TokenSource.DEPLOYMENT_FALLBACK)) is True

    async def test_empty_deployment_token_is_not_dispatched(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """匿名者的那一格：部署级令牌为空，握手什么都没出示"""
        set_deployment_token(monkeypatch, "")
        assert await allowed(record=handshake("")) is False

    async def test_no_handshake_is_not_dispatched(self, db) -> None:
        assert await allowed(record=None) is False

    async def test_an_awaiting_instance_token_is_not_the_deployment_token(self, db) -> None:
        record = handshake("awaiting-own-token", source=TokenSource.AWAITING_ACCOUNT, instance_key="napcat_new")
        assert await allowed(record=record) is False

    async def test_a_connection_on_an_old_deployment_token_is_not_dispatched(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """部署级令牌换过之后，旧连接的指纹对不上，判为暂停"""
        record = handshake(DEPLOYMENT_TOKEN)
        set_deployment_token(monkeypatch, "rotated-token")
        assert await allowed(record=record) is False

    async def test_an_exposed_deployment_token_is_not_dispatched(self, db) -> None:
        record_exposed_tokens([DEPLOYMENT_TOKEN], reason="用例")
        assert await allowed(record=handshake(DEPLOYMENT_TOKEN)) is False

    async def test_a_tenant_instance_on_the_deployment_token_exposes_it_first(self, db) -> None:
        """判定时补记：此刻有租户实例靠部署级令牌认证，部署级令牌就在租户手里"""
        await add_instance("tenant_dep", status="online", account="20005", tenant_id=TENANT_ID)
        assert await allowed(record=handshake(DEPLOYMENT_TOKEN)) is False

    async def test_an_unusable_exposure_record_is_not_dispatched(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        def disk_full(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        exposure_file().unlink()
        monkeypatch.setattr(token_exposure, "_write", disk_full)
        assert await allowed(record=handshake(DEPLOYMENT_TOKEN)) is False

    @pytest.mark.parametrize("instance_key", [PLATFORM, "tenant_inst"])
    async def test_claimed_instances_are_not_affected(self, db, monkeypatch: pytest.MonkeyPatch, instance_key: str) -> None:
        set_deployment_token(monkeypatch, "")
        assert await allowed(instance_key, record=None) is True

    async def test_disabled_instances_are_still_refused(self, db) -> None:
        assert await allowed("off", record=handshake(DEPLOYMENT_TOKEN)) is False

    async def test_a_failing_check_counts_as_not_dispatched(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        async def broken() -> str:
            raise RuntimeError("坏了")

        monkeypatch.setattr(unclaimed_dispatch, "unclaimed_pause_reason", broken)
        assert await allowed(record=handshake(DEPLOYMENT_TOKEN)) is False


class TestWarningIsRateLimited:
    async def test_one_warning_per_instance(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: List[str] = []

        class _Recorder:
            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

            def warning(self, message: str, *_a: Any, **_k: Any) -> None:
                warnings.append(message)

        monkeypatch.setattr(unclaimed_dispatch, "logger", _Recorder())
        set_deployment_token(monkeypatch, "")

        for _ in range(3):
            assert await allowed(record=None) is False

        assert len(warnings) == 1
        assert "部署级令牌为空" in warnings[0] and ANON in warnings[0]

    async def test_the_tracking_table_has_a_limit(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """换号刷行时每个号都是一个新实例：记着的实例数不随之无限变大"""
        monkeypatch.setattr(unclaimed_dispatch, "_WARN_TRACK_LIMIT", 3)
        set_deployment_token(monkeypatch, "")
        for index in range(6):
            await add_instance(f"spam{index}", status="unclaimed", account=f"4000{index}")
            assert await allowed(f"spam{index}", record=None) is False

        assert list(unclaimed_dispatch._last_warned) == [(ONEBOT, "spam3"), (ONEBOT, "spam4"), (ONEBOT, "spam5")]  # noqa: SLF001


# ---------------------------------------------------------------------------
# collector：不建频道行、不落库、不识别命令
# ---------------------------------------------------------------------------


class FakeAdapter:
    key = ONEBOT

    def __init__(self) -> None:
        self.command_probes: List[str] = []
        self.executed: List[str] = []

    def build_chat_key(self, channel_id: str, instance_key: str = "") -> str:
        return f"{self.key}-{instance_key}-{channel_id}" if instance_key else f"{self.key}-{channel_id}"

    def detect_command(self, text: str) -> Optional[Tuple[str, str]]:
        self.command_probes.append(text)
        return ("na_info", "") if text.startswith("/na_info") else None

    async def try_handle_wait_input(self, **_kwargs: Any) -> bool:
        return False

    async def execute_command(self, **kwargs: Any) -> None:
        self.executed.append(kwargs["command_name"])

    async def get_user_command_permission(self, *_args: Any) -> Any:
        from nekro_agent.services.command.base import CommandPermission

        return CommandPermission.USER


def inbound(instance_key: str, text: str = "/na_info") -> Tuple[PlatformChannel, PlatformUser, PlatformMessage]:
    return (
        PlatformChannel(channel_id=CHANNEL_ID, channel_name="", channel_type=ChatType.GROUP, instance_key=instance_key),
        PlatformUser(platform_name="qq", user_id="10001", user_name="tester"),
        PlatformMessage(message_id="1", sender_id="10001", sender_name="tester", content_text=text),
    )


class TestCollector:
    async def test_anonymous_message_leaves_no_trace(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        set_deployment_token(monkeypatch, "")
        adapter = FakeAdapter()

        with connection(handshake("")):
            await collect_message(adapter, *inbound(ANON))  # type: ignore[arg-type]

        assert await DBChatChannel.all().count() == 0
        assert await DBChatMessage.all().count() == 0
        assert adapter.command_probes == []

    async def test_control_deployment_token_connection_flows_through(self, db) -> None:
        adapter = FakeAdapter()

        with connection(handshake(DEPLOYMENT_TOKEN)):
            await collect_message(adapter, *inbound(ANON))  # type: ignore[arg-type]

        assert await DBChatChannel.filter(chat_key=f"{ONEBOT}-{ANON}-{CHANNEL_ID}").count() == 1
        assert adapter.executed == ["na_info"]

    async def test_claiming_lets_the_same_connection_through(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """认领之后不用重连：本项只拦未认领，同一条连接的下一条消息就照常分发"""
        from nekro_agent.services.console.instance_manager import claim_instance

        set_deployment_token(monkeypatch, "")
        adapter = FakeAdapter()
        anonymous = handshake("")
        with connection(anonymous):
            await collect_message(adapter, *inbound(ANON))  # type: ignore[arg-type]
        assert adapter.executed == []

        await claim_instance(await DBAdapterInstance.get(instance_key=ANON), "我的机器人")

        with connection(anonymous):
            await collect_message(adapter, *inbound(ANON))  # type: ignore[arg-type]
        assert adapter.executed == ["na_info"]

    async def test_a_closed_channel_still_answers_na_info_on_a_claimed_instance(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """频道关着时内置命令照常应答是设计行为：本项只看实例是否已认领，不碰频道开关"""
        set_deployment_token(monkeypatch, "")
        chat_key = f"{ONEBOT}-{PLATFORM}-{CHANNEL_ID}"
        await DBChatChannel.create(
            adapter_key=ONEBOT, instance_key=PLATFORM, channel_id=CHANNEL_ID, channel_type=ChatType.GROUP.value,
            channel_name="", chat_key=chat_key, is_active=False, observe_mode=False, data="{}",
        )
        adapter = FakeAdapter()

        await collect_message(adapter, *inbound(PLATFORM))  # type: ignore[arg-type]

        assert adapter.executed == ["na_info"]


# ---------------------------------------------------------------------------
# HTTP 上报：同一条规则，按单次请求判
# ---------------------------------------------------------------------------


def _http_scope(self_id: str, token: Optional[str]) -> Dict[str, Any]:
    headers: List[Tuple[bytes, bytes]] = [(b"host", b"nekro_agent:8021"), (b"x-self-id", self_id.encode())]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "http", "method": "POST", "path": "/onebot/v11/", "headers": headers}


async def dispatch_through_gate(token: Optional[str]) -> List[bool]:
    """走一遍真实的握手中间件，在 NoneBot 派发事件的那种任务里问闸门"""
    seen: List[bool] = []

    async def downstream(_scope: MutableMapping[str, Any], _receive: Any, _send: Any) -> None:
        async def handle_event() -> bool:
            return await is_inbound_dispatch_allowed(ONEBOT, ANON)

        seen.append(await asyncio.create_task(handle_event()))

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request"}

    async def send(_message: Dict[str, Any]) -> None:
        return None

    await OneBotAccessTokenMiddleware(downstream)(_http_scope(ANON_ACCOUNT, token), receive, send)
    return seen


class TestHttpReport:
    async def test_report_with_the_deployment_token_is_dispatched(self, db) -> None:
        assert await dispatch_through_gate(DEPLOYMENT_TOKEN) == [True]

    async def test_anonymous_report_is_not_dispatched(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        set_deployment_token(monkeypatch, "")
        assert await dispatch_through_gate(None) == [False]


# ---------------------------------------------------------------------------
# 总后台的提示
# ---------------------------------------------------------------------------


class TestPauseReason:
    async def test_empty_deployment_token(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        set_deployment_token(monkeypatch, "")
        assert await unclaimed_pause_reason() == "部署级令牌为空"

    async def test_exposed_deployment_token(self, db) -> None:
        record_exposed_tokens([DEPLOYMENT_TOKEN], reason="用例")
        assert await unclaimed_pause_reason() == "部署级令牌曾对租户可见"

    async def test_control_usable_deployment_token(self, db) -> None:
        assert await unclaimed_pause_reason() == ""

    async def test_console_list_carries_it(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        import datetime

        from nekro_agent.models.db_user import DBUser
        from nekro_agent.routers.console import instances as instances_router
        from nekro_agent.schemas.tenant import TenantRole
        from nekro_agent.services.user.role import Role

        owner = DBUser(
            username="owner", password="", adapter_key="", platform_userid="", perm_level=Role.Super,
            login_time=datetime.datetime.now(datetime.timezone.utc), tenant_id=None,
            tenant_role=TenantRole.PLATFORM_OWNER.value,
        )
        owner.id = 1
        set_deployment_token(monkeypatch, "")
        response = await instances_router.get_console_unclaimed_instances(_current_user=owner)

        assert [(item.instance_key, item.dispatch_paused, item.dispatch_paused_reason) for item in response.items] == [
            (ANON, True, "部署级令牌为空"),
        ]

        # 判不出来（比如某行租户实例的元数据坏了）时清单照常返回，按「暂停」显示
        await add_instance("tenant_bad", status="online", account="20007", tenant_id=TENANT_ID)
        await DBAdapterInstance.filter(instance_key="tenant_bad").update(metadata_json=json.dumps({"webui_port": "abc"}))
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        response = await instances_router.get_console_unclaimed_instances(_current_user=owner)

        assert [(item.dispatch_paused, item.dispatch_paused_reason) for item in response.items] == [(True, "判定出错")]


# ---------------------------------------------------------------------------
# 升级迁移与启动告警
# ---------------------------------------------------------------------------


async def add_history(instance_key: str) -> None:
    await DBChatMessage.create(
        sender_id="10001", sender_name="x", sender_nickname="x", is_tome=0, is_recalled=False, adapter_key=ONEBOT,
        instance_key=instance_key, message_id="m1", chat_key=f"{ONEBOT}-{instance_key}-{CHANNEL_ID}",
        chat_type=ChatType.GROUP, platform_userid="10001", content_text="hi", content_data="[]", raw_cq_code="",
        ext_data="", send_timestamp=0,
    )


class TestUpgradeMigration:
    async def test_unclaimed_with_history_is_claimed_once(self, db) -> None:
        await add_history(ANON)
        await add_instance("anon_quiet", status="unclaimed", account="30002")

        assert await auto_claim_unclaimed_with_history() == [ANON]

        claimed = await DBAdapterInstance.get(instance_key=ANON)
        assert claimed.status != "unclaimed" and claimed.display_name == ANON and claimed.enabled
        event = await DBAdapterInstanceEvent.filter(instance_id=claimed.id).order_by("-id").first()
        assert event is not None and "升级时自动认领" in event.message
        assert (await DBAdapterInstance.get(instance_key="anon_quiet")).status == "unclaimed"

        marker = json.loads(unclaimed_dispatch._migration_file().read_text(encoding="utf-8"))  # noqa: SLF001
        assert marker == {"done": True, "claimed": [ANON]}

    async def test_it_runs_only_once(self, db) -> None:
        assert await auto_claim_unclaimed_with_history() == []
        await add_history(ANON)

        assert await auto_claim_unclaimed_with_history() == []
        assert (await DBAdapterInstance.get(instance_key=ANON)).status == "unclaimed"

    async def test_a_lost_marker_reruns_without_touching_claimed_ones(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        real_replace = unclaimed_dispatch.os.replace

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        await add_history(ANON)
        monkeypatch.setattr(unclaimed_dispatch.os, "replace", refuse)
        assert await auto_claim_unclaimed_with_history() == [ANON]
        assert not unclaimed_dispatch._migration_file().exists()  # noqa: SLF001
        monkeypatch.setattr(unclaimed_dispatch.os, "replace", real_replace)

        before = await DBAdapterInstanceEvent.all().count()
        assert await auto_claim_unclaimed_with_history() == []
        assert await DBAdapterInstanceEvent.all().count() == before

    async def test_one_failing_row_does_not_stop_the_others_and_is_retried(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.adapter_instance_service import adapter_instance_service

        await add_instance("anon_b", status="unclaimed", account="30002")
        await add_history(ANON)
        await add_history("anon_b")
        real_set_status = adapter_instance_service.set_status

        async def flaky(instance: DBAdapterInstance, *args: Any, **kwargs: Any) -> Any:
            if instance.instance_key == ANON:
                raise RuntimeError("数据库抖了一下")
            return await real_set_status(instance, *args, **kwargs)

        monkeypatch.setattr(adapter_instance_service, "set_status", flaky)
        assert await auto_claim_unclaimed_with_history() == ["anon_b"]
        # 有一行失败：标记不写，下次启动重跑剩下的
        assert not unclaimed_dispatch._migration_file().exists()  # noqa: SLF001

        monkeypatch.setattr(adapter_instance_service, "set_status", real_set_status)
        assert await auto_claim_unclaimed_with_history() == [ANON]
        assert unclaimed_dispatch._migration_file().exists()  # noqa: SLF001

    async def test_an_auto_claimed_instance_is_not_a_super_user_source_without_a_token(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.user.super_users import is_trusted_super_user_source

        set_deployment_token(monkeypatch, "")
        await add_history(ANON)
        await auto_claim_unclaimed_with_history()

        with connection(handshake("", instance_key=ANON)):
            assert await is_trusted_super_user_source(ONEBOT, ANON) is False

    def test_startup_claims_before_scanning_and_warning(self) -> None:
        import inspect

        import nekro_agent

        source = inspect.getsource(nekro_agent)
        assert source.index("await auto_claim_unclaimed_with_history()") < source.index("await scan_tenant_token_exposure()")
        assert source.index("await scan_tenant_token_exposure()") < source.index("await log_super_user_warnings()")


class TestStartupWarning:
    @staticmethod
    async def warnings() -> List[str]:
        from nekro_agent.services.user.super_users import collect_super_user_warnings

        return await collect_super_user_warnings()

    async def test_empty_deployment_token_is_reported_once_even_without_super_users(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", [])
        set_deployment_token(monkeypatch, "")

        reported = await self.warnings()
        assert len(reported) == 1
        assert "ONEBOT_ACCESS_TOKEN 为空" in reported[0] and "待认领" in reported[0]

    async def test_with_super_users_it_is_still_one_line(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", ["10086"])
        set_deployment_token(monkeypatch, "")
        await add_instance("platform_dep", status="online", account="20006")

        about_token = [item for item in await self.warnings() if "ONEBOT_ACCESS_TOKEN 为空" in item]
        assert len(about_token) == 1
        assert "待认领" in about_token[0] and "platform_dep" in about_token[0]

    async def test_control_nothing_without_super_users_and_with_a_token(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", [])
        assert await self.warnings() == []
