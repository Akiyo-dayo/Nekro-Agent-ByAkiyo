"""主动核对 Bot 在群：解析、按实例比对、fail-safe、调度与后台接口

这里钉住四层：

1. 协议端返回的群列表怎么解析，什么情况整份不采信；
2. 单个实例的核对如何只写标记、如何按实例隔离、失败时零写库；
3. 入站清标记、手动停用与自动停用标记的边界；
4. 后台列表筛选、手动核对、批量停用预览与执行的作用域。
"""

import asyncio
import datetime
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError
from nekro_agent.adapters.onebot_v11.tools import bot_membership_check as membership_check
from nekro_agent.adapters.onebot_v11.tools.bot_membership import restore_after_bot_returned
from nekro_agent.adapters.onebot_v11.tools.bot_membership_check import (
    BotMembershipScheduler,
    MembershipCheckOutcome,
    MembershipCheckResult,
    check_instance_membership,
    is_check_due,
    parse_group_list,
    reset_membership_check_state,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import (
    CHANNEL_DATA_AUTO_DISABLE_KEY,
    CHANNEL_DATA_BOT_MEMBERSHIP_KEY,
    BotMembershipSource,
    BotMembershipStatus,
    ChannelAutoDisableReason,
    DBChatChannel,
)
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import chat_channel as chat_channel_router
from nekro_agent.routers.chat_channel import (
    BotMembershipCheckRequest,
    BotMembershipDisableRequest,
    check_bot_membership,
    disable_not_in_group,
    get_chat_channel_detail,
    get_chat_channel_list,
    preview_disable_not_in_group,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
ACCOUNT_A = "10001"
ACCOUNT_B = "10002"
NOW = 100000

TENANT_A = 1
TENANT_B = 2


@pytest.fixture(autouse=True)
def _reset_check_state():
    reset_membership_check_state()
    yield
    reset_membership_check_state()


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class _LogRecorder:
    """记录核对模块真正打出去的 WARNING（loguru 不经过 stdlib logging）"""

    def __init__(self) -> None:
        self.messages: List[str] = []

    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None

    def warning(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.messages.append(message)


@pytest.fixture
def log_recorder(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(membership_check, "logger", recorder)
    return recorder


class FakeBot:
    """只实现 call_api 的替身，并记下每次调用的 action 名与起止时间"""

    def __init__(
        self,
        groups: object = None,
        *,
        error: Optional[Exception] = None,
        delay: float = 0.0,
    ) -> None:
        self.groups = groups
        self.error = error
        self.delay = delay
        self.calls: List[str] = []
        self.intervals: List[tuple] = []

    async def call_api(self, action: str) -> object:
        self.calls.append(action)
        started = time.monotonic()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.groups
        finally:
            self.intervals.append((started, time.monotonic()))


def _groups(*group_ids: int) -> List[Dict[str, int]]:
    return [{"group_id": group_id} for group_id in group_ids]


def _install_bot(
    monkeypatch: pytest.MonkeyPatch,
    bot: FakeBot,
    *,
    account_id: Optional[str] = ACCOUNT_A,
) -> None:
    monkeypatch.setattr(membership_check, "lookup_cached_account_id", lambda _key: None)

    async def _resolve(_key: str) -> Optional[str]:
        return account_id

    monkeypatch.setattr(membership_check, "resolve_account_id", _resolve)
    monkeypatch.setattr(membership_check, "get_bot", lambda _account: bot)


async def _make_instance(
    instance_key: str,
    *,
    account_id: str = ACCOUNT_A,
    adapter_key: str = ADAPTER,
    enabled: bool = True,
    status: str = "online",
    tenant_id: Optional[int] = None,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        provider_account_id=account_id,
        enabled=enabled,
        status=status,
        tenant_id=tenant_id,
    )


async def _make_channel(
    instance_key: str,
    channel_id: str,
    *,
    adapter_key: str = ADAPTER,
    is_active: bool = True,
    observe_mode: bool = False,
    data: str = "{}",
) -> DBChatChannel:
    chat_key = f"{adapter_key}-{instance_key}-{channel_id}"
    return await DBChatChannel.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        channel_id=channel_id,
        chat_key=chat_key,
        channel_type="group" if channel_id.startswith("group_") else "private",
        is_active=is_active,
        observe_mode=observe_mode,
        data=data,
    )


async def _mark(
    channel: DBChatChannel,
    status: BotMembershipStatus,
    *,
    account_id: str = ACCOUNT_A,
    checked_at: int = NOW,
) -> None:
    await channel.record_bot_membership(
        status,
        BotMembershipSource.CHECK,
        account_id=account_id,
        now=checked_at,
    )


def _data(channel: DBChatChannel) -> Dict[str, Any]:
    return json.loads(channel.data)


def _snapshot(channels: List[DBChatChannel]) -> Dict[int, tuple]:
    return {
        channel.id: (
            channel.data,
            channel.is_active,
            channel.observe_mode,
            channel.update_time,
        )
        for channel in channels
    }


async def _add_message(chat_key: str, *, send_timestamp: int, sender_id: str = "20002") -> None:
    await DBChatMessage.create(
        sender_id=sender_id,
        sender_name="发送者",
        sender_nickname="发送者",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key="inst_a",
        message_id="",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=sender_id,
        content_text="内容",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=send_timestamp,
    )


# ---------------------------------------------------------------------------
# 1. 群列表解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param([{"group_id": 100001}], {100001}, id="int"),
        pytest.param([{"group_id": "100002"}], {100002}, id="numeric-string"),
        pytest.param([], set(), id="empty"),
        pytest.param([{"group_id": 100001}, {"group_id": "100002"}], {100001, 100002}, id="mixed"),
    ],
)
def test_parse_group_list_accepts_valid_shapes(raw: object, expected: set) -> None:
    assert parse_group_list(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"group_id": 100001}, id="not-list"),
        pytest.param([100001], id="item-not-dict"),
        pytest.param([{"name": "群"}], id="missing-group-id"),
        pytest.param([{"group_id": True}], id="bool"),
        pytest.param([{"group_id": "abc"}], id="non-numeric-string"),
        pytest.param([{"group_id": -100001}], id="negative"),
        pytest.param([{"group_id": 100001}, {"group_id": None}], id="one-bad-item-invalidates-all"),
    ],
)
def test_parse_group_list_rejects_untrustworthy_shapes(raw: object) -> None:
    assert parse_group_list(raw) is None


# ---------------------------------------------------------------------------
# 2-4. 正常核对、按实例隔离、旧标记清除
# ---------------------------------------------------------------------------


async def test_normal_check_marks_only_this_instance(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    await _make_instance("inst_b", account_id=ACCOUNT_B)

    in_group = await _make_channel("inst_a", "group_100001")
    not_in_group = await _make_channel("inst_a", "group_100002")
    non_numeric = await _make_channel("inst_a", "group_tenant_gate_test", data=json.dumps({"custom_channel_name": "非群号"}))
    private = await _make_channel("inst_a", "private_100003", data=json.dumps({"custom_channel_name": "私聊"}))
    other_instance = await _make_channel("inst_b", "group_100002", data=json.dumps({"custom_channel_name": "B 的群"}))

    # 用一次真实查询拿到落库后的 update_time，避免用建行对象上的内存值比较
    all_before = await DBChatChannel.all()
    before = _snapshot(all_before)

    bot = FakeBot(_groups(100001))
    _install_bot(monkeypatch, bot)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.CHECKED
    assert result.group_count == 1
    assert result.channel_count == 2
    assert result.not_in_group_count == 1
    assert result.newly_marked == 1
    assert result.cleared == 0
    assert result.skipped_non_numeric == 1
    assert result.account_id == ACCOUNT_A
    assert bot.calls == ["get_group_list"]

    await in_group.refresh_from_db()
    await not_in_group.refresh_from_db()
    await non_numeric.refresh_from_db()
    await private.refresh_from_db()
    await other_instance.refresh_from_db()

    info_in = in_group.get_bot_membership_info()
    assert info_in is not None
    assert info_in.status is BotMembershipStatus.IN_GROUP
    assert info_in.source is BotMembershipSource.CHECK
    assert info_in.account_id == ACCOUNT_A
    assert info_in.checked_at == NOW

    info_out = not_in_group.get_bot_membership_info()
    assert info_out is not None
    assert info_out.status is BotMembershipStatus.NOT_IN_GROUP
    assert info_out.source is BotMembershipSource.CHECK
    assert info_out.account_id == ACCOUNT_A
    assert info_out.checked_at == NOW

    assert CHANNEL_DATA_BOT_MEMBERSHIP_KEY not in _data(non_numeric)
    assert CHANNEL_DATA_BOT_MEMBERSHIP_KEY not in _data(private)
    assert CHANNEL_DATA_BOT_MEMBERSHIP_KEY not in _data(other_instance)

    after = _snapshot(await DBChatChannel.all())
    for channel_id, snapshot in before.items():
        if channel_id in {in_group.id, not_in_group.id}:
            # 只有 data 允许变化；启停、旁观、更新时间都不该被核对碰到
            assert after[channel_id][1:] == snapshot[1:]
        else:
            assert after[channel_id] == snapshot


async def test_instances_are_compared_separately(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    await _make_instance("inst_b", account_id=ACCOUNT_B)
    channel_a = await _make_channel("inst_a", "group_100002")
    channel_b = await _make_channel("inst_b", "group_100002")

    bot_a = FakeBot(_groups(100001))
    _install_bot(monkeypatch, bot_a)
    await check_instance_membership("inst_a", now=NOW)

    bot_b = FakeBot(_groups(100002))
    _install_bot(monkeypatch, bot_b, account_id=ACCOUNT_B)
    await check_instance_membership("inst_b", now=NOW)

    await channel_a.refresh_from_db()
    await channel_b.refresh_from_db()

    info_a = channel_a.get_bot_membership_info()
    info_b = channel_b.get_bot_membership_info()
    assert info_a is not None and info_a.status is BotMembershipStatus.NOT_IN_GROUP
    assert info_b is not None and info_b.status is BotMembershipStatus.IN_GROUP


async def test_old_marker_is_cleared_when_group_comes_back(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    channel = await _make_channel("inst_a", "group_100002")
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW - 1000)

    bot = FakeBot(_groups(100002))
    _install_bot(monkeypatch, bot)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.CHECKED
    assert result.cleared == 1
    assert result.newly_marked == 0
    await channel.refresh_from_db()
    info = channel.get_bot_membership_info()
    assert info is not None and info.status is BotMembershipStatus.IN_GROUP


# ---------------------------------------------------------------------------
# 5. fail-safe：每一轮都先放好两种旧标记，断言一字未改
# ---------------------------------------------------------------------------


async def _seed_failsafe_pair() -> tuple:
    await _make_instance("inst_a")
    marked = await _make_channel("inst_a", "group_100002", data=json.dumps({"custom_channel_name": "旧标记"}))
    in_group = await _make_channel("inst_a", "group_100001", data=json.dumps({"custom_channel_name": "在群"}))
    await _mark(marked, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW - 100)
    await _mark(in_group, BotMembershipStatus.IN_GROUP, checked_at=NOW - 100)
    await marked.refresh_from_db()
    await in_group.refresh_from_db()
    return marked, in_group


async def test_call_failure_changes_nothing(db, monkeypatch: pytest.MonkeyPatch, log_recorder: _LogRecorder) -> None:
    marked, in_group = await _seed_failsafe_pair()
    before = (marked.data, in_group.data)

    bot = FakeBot(error=RuntimeError("协议端炸了"))
    _install_bot(monkeypatch, bot)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.CALL_FAILED
    await marked.refresh_from_db()
    await in_group.refresh_from_db()
    assert (marked.data, in_group.data) == before
    assert log_recorder.messages


async def test_timeout_changes_nothing(db, monkeypatch: pytest.MonkeyPatch, log_recorder: _LogRecorder) -> None:
    marked, in_group = await _seed_failsafe_pair()
    before = (marked.data, in_group.data)
    monkeypatch.setattr(membership_check, "GROUP_LIST_TIMEOUT_SECONDS", 0.01)

    bot = FakeBot(_groups(100001), delay=0.2)
    _install_bot(monkeypatch, bot)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.CALL_FAILED
    # 超时异常的 str() 是空串：后台要能看到原因，不能只剩一句空的「失败」
    assert result.message == "TimeoutError"
    await marked.refresh_from_db()
    await in_group.refresh_from_db()
    assert (marked.data, in_group.data) == before
    assert log_recorder.messages


async def test_empty_group_list_changes_nothing(db, monkeypatch: pytest.MonkeyPatch, log_recorder: _LogRecorder) -> None:
    marked, in_group = await _seed_failsafe_pair()
    before = (marked.data, in_group.data)

    _install_bot(monkeypatch, FakeBot([]))

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.EMPTY
    await marked.refresh_from_db()
    await in_group.refresh_from_db()
    assert (marked.data, in_group.data) == before
    assert log_recorder.messages


async def test_malformed_group_list_changes_nothing(db, monkeypatch: pytest.MonkeyPatch, log_recorder: _LogRecorder) -> None:
    marked, in_group = await _seed_failsafe_pair()
    before = (marked.data, in_group.data)

    _install_bot(monkeypatch, FakeBot({"x": 1}))

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.MALFORMED
    await marked.refresh_from_db()
    await in_group.refresh_from_db()
    assert (marked.data, in_group.data) == before
    assert log_recorder.messages


async def test_recent_inbound_in_a_missing_group_changes_nothing(
    db,
    monkeypatch: pytest.MonkeyPatch,
    log_recorder: _LogRecorder,
) -> None:
    marked, in_group = await _seed_failsafe_pair()
    before = (marked.data, in_group.data)
    # 「不在群」的频道十分钟前还有人说话
    await _add_message(marked.chat_key, send_timestamp=NOW - 600)

    _install_bot(monkeypatch, FakeBot(_groups(100001)))

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.INCOMPLETE
    assert marked.chat_key in result.message
    await marked.refresh_from_db()
    await in_group.refresh_from_db()
    assert (marked.data, in_group.data) == before
    assert log_recorder.messages


# ---------------------------------------------------------------------------
# 6. 群数骤减
# ---------------------------------------------------------------------------


async def test_group_count_drop_is_distrusted_once_then_accepted(
    db,
    monkeypatch: pytest.MonkeyPatch,
    log_recorder: _LogRecorder,
) -> None:
    await _make_instance("inst_a")
    channel = await _make_channel("inst_a", "group_100001")
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW - 100)
    await channel.refresh_from_db()
    before = channel.data

    _install_bot(monkeypatch, FakeBot(_groups(*range(100001, 100011))))
    first = await check_instance_membership("inst_a", now=NOW)
    assert first.outcome is MembershipCheckOutcome.CHECKED
    assert first.group_count == 10
    await channel.refresh_from_db()
    assert channel.get_bot_membership_info().status is BotMembershipStatus.IN_GROUP

    # 群数从 10 骤减到 3：第一次先不采信
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW)
    await channel.refresh_from_db()
    before = channel.data
    _install_bot(monkeypatch, FakeBot(_groups(100001, 100002, 100003)))
    second = await check_instance_membership("inst_a", now=NOW)
    assert second.outcome is MembershipCheckOutcome.INCOMPLETE
    await channel.refresh_from_db()
    assert channel.data == before

    # 连续第二次还是 3 个：按它标记
    third = await check_instance_membership("inst_a", now=NOW)
    assert third.outcome is MembershipCheckOutcome.CHECKED
    await channel.refresh_from_db()
    assert channel.get_bot_membership_info().status is BotMembershipStatus.IN_GROUP


async def test_new_account_does_not_inherit_old_baseline(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    channel = await _make_channel("inst_a", "group_100001")

    _install_bot(monkeypatch, FakeBot(_groups(*range(100001, 100011))))
    await check_instance_membership("inst_a", now=NOW)

    # 换成账号 10002 后只返回 1 个群：旧账号的 10 个不该拿来触发「骤减」
    _install_bot(monkeypatch, FakeBot(_groups(100002)), account_id=ACCOUNT_B)
    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.CHECKED
    assert result.account_id == ACCOUNT_B
    await channel.refresh_from_db()
    assert channel.get_bot_membership_info().status is BotMembershipStatus.NOT_IN_GROUP


# ---------------------------------------------------------------------------
# 7. 离线
# ---------------------------------------------------------------------------


async def test_missing_account_is_offline_and_writes_nothing(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    channel = await _make_channel("inst_a", "group_100001")
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP)
    await channel.refresh_from_db()
    before = channel.data

    _install_bot(monkeypatch, FakeBot(_groups(100001)), account_id=None)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.OFFLINE
    await channel.refresh_from_db()
    assert channel.data == before


async def test_offline_bot_is_offline_and_writes_nothing(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    channel = await _make_channel("inst_a", "group_100001")
    await _mark(channel, BotMembershipStatus.IN_GROUP)
    await channel.refresh_from_db()
    before = channel.data

    monkeypatch.setattr(membership_check, "lookup_cached_account_id", lambda _key: None)

    async def _resolve(_key: str) -> str:
        return ACCOUNT_A

    monkeypatch.setattr(membership_check, "resolve_account_id", _resolve)

    def _raise(_account: str):
        raise BotInstanceOfflineError(_account)

    monkeypatch.setattr(membership_check, "get_bot", _raise)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.OFFLINE
    await channel.refresh_from_db()
    assert channel.data == before


# ---------------------------------------------------------------------------
# 8. 并发
# ---------------------------------------------------------------------------


async def test_same_instance_checks_are_serialized(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    await _make_channel("inst_a", "group_100001")
    bot = FakeBot(_groups(100001), delay=0.05)
    _install_bot(monkeypatch, bot)

    await asyncio.gather(
        check_instance_membership("inst_a", now=NOW),
        check_instance_membership("inst_a", now=NOW),
    )

    assert len(bot.intervals) == 2
    first, second = sorted(bot.intervals, key=lambda interval: interval[0])
    assert second[0] >= first[1]


# ---------------------------------------------------------------------------
# 9. 入站清标记
# ---------------------------------------------------------------------------


async def test_inbound_clears_not_in_group_marker(db) -> None:
    channel = await _make_channel("inst_a", "group_100001")
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP, account_id=ACCOUNT_A)
    await channel.refresh_from_db()

    restored = await restore_after_bot_returned(channel, "群消息")

    assert restored is False
    await channel.refresh_from_db()
    info = channel.get_bot_membership_info()
    assert info is not None
    assert info.status is BotMembershipStatus.IN_GROUP
    assert info.source is BotMembershipSource.INBOUND
    assert info.account_id == ""


async def test_inbound_without_marker_does_not_write(db, monkeypatch: pytest.MonkeyPatch) -> None:
    channel = await _make_channel("inst_a", "group_100001", data=json.dumps({"custom_channel_name": "群"}))
    await channel.refresh_from_db()
    before = channel.data
    saves: List[int] = []

    original_save = DBChatChannel.save

    async def _counting_save(self, *args: Any, **kwargs: Any):
        saves.append(self.id)
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(DBChatChannel, "save", _counting_save)

    assert await restore_after_bot_returned(channel, "群消息") is False
    assert saves == []
    await channel.refresh_from_db()
    assert channel.data == before


async def test_inbound_still_restores_auto_disabled_channel(db) -> None:
    channel = await _make_channel("inst_a", "group_100001")
    assert await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP) is True
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP, account_id=ACCOUNT_A)
    await channel.refresh_from_db()
    assert channel.is_active is False

    restored = await restore_after_bot_returned(channel, "群消息")

    assert restored is True
    await channel.refresh_from_db()
    assert channel.is_active is True
    assert channel.get_auto_disable_info() is None
    info = channel.get_bot_membership_info()
    assert info is not None and info.status is BotMembershipStatus.IN_GROUP


# ---------------------------------------------------------------------------
# 10. 模型边界
# ---------------------------------------------------------------------------


async def test_recording_membership_keeps_auto_disable_marker(db) -> None:
    channel = await _make_channel("inst_a", "group_100001")

    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP, "被移出群")
    assert channel.get_auto_disable_info() is not None

    await channel.record_bot_membership(
        BotMembershipStatus.IN_GROUP,
        BotMembershipSource.CHECK,
        account_id=ACCOUNT_A,
        now=NOW,
    )

    assert channel.get_auto_disable_info() is not None
    assert channel.is_active is False


async def test_manual_disable_keeps_membership_marker(db) -> None:
    channel = await _make_channel("inst_a", "group_100001")
    await _mark(channel, BotMembershipStatus.NOT_IN_GROUP)

    await channel.set_channel_status("disabled")

    assert channel.is_active is False
    info = channel.get_bot_membership_info()
    assert info is not None
    assert info.status is BotMembershipStatus.NOT_IN_GROUP


# ---------------------------------------------------------------------------
# 11. 路由
# ---------------------------------------------------------------------------


def _make_user(
    *,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
    username: str,
) -> DBUser:
    return DBUser(
        username=username,
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def _platform_owner() -> DBUser:
    return _make_user(
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Super,
        username="platform",
    )


def _tenant_admin(tenant_id: int = TENANT_A) -> DBUser:
    return _make_user(
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        perm_level=Role.Admin,
        username=f"tenant-{tenant_id}",
    )


async def _seed_routes() -> SimpleNamespace:
    await DBTenant.create(id=TENANT_A, name="acme", display_name="acme")
    await DBTenant.create(id=TENANT_B, name="rival", display_name="rival")

    await _make_instance("inst_a", tenant_id=TENANT_A)
    await _make_instance("inst_b", account_id=ACCOUNT_B, tenant_id=TENANT_B)
    await _make_instance("wx_a", account_id="10003", adapter_key="wechat_ilink_multi", tenant_id=TENANT_A)

    a1 = await _make_channel("inst_a", "group_100001")
    a2 = await _make_channel("inst_a", "group_100002", data=json.dumps({"custom_channel_name": "在群"}))
    a3 = await _make_channel("inst_a", "group_100003", is_active=False)
    a4 = await _make_channel("inst_a", "group_100004", data=json.dumps({"custom_channel_name": "无标记"}))
    b1 = await _make_channel("inst_b", "group_200001")

    await _mark(a1, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW)
    await _mark(a2, BotMembershipStatus.IN_GROUP, checked_at=NOW)
    await _mark(a3, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW)
    await _mark(b1, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW)

    return SimpleNamespace(a1=a1, a2=a2, a3=a3, a4=a4, b1=b1)


async def test_list_filters_by_bot_membership_and_exposes_marker(db) -> None:
    await _seed_routes()
    platform = _platform_owner()

    response = await get_chat_channel_list(
        page=1,
        page_size=50,
        bot_membership="not_in_group",
        _current_user=platform,
    )
    assert response.total == 3
    assert {item.chat_key for item in response.items} == {
        f"{ADAPTER}-inst_a-group_100001",
        f"{ADAPTER}-inst_a-group_100003",
        f"{ADAPTER}-inst_b-group_200001",
    }
    assert all(item.bot_membership is not None for item in response.items)
    assert all(item.bot_membership.status == "not_in_group" for item in response.items)

    unfiltered = await get_chat_channel_list(page=1, page_size=50, _current_user=platform)
    marked_item = next(item for item in unfiltered.items if item.chat_key.endswith("group_100001"))
    assert marked_item.bot_membership is not None
    assert marked_item.bot_membership.checked_at == NOW

    detail = await get_chat_channel_detail(marked_item.chat_key, _current_user=platform)
    assert detail.bot_membership is not None
    assert detail.bot_membership.status == "not_in_group"


async def test_list_rejects_unknown_bot_membership_filter(db) -> None:
    await _seed_routes()
    with pytest.raises(ValidationError):
        await get_chat_channel_list(
            page=1,
            page_size=50,
            bot_membership="in_group",
            _current_user=_platform_owner(),
        )


async def test_manual_check_respects_instance_access(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_routes()

    async def _fake_check(instance_key: str, **_kwargs: Any) -> MembershipCheckResult:
        return MembershipCheckResult(
            instance_key=instance_key,
            outcome=MembershipCheckOutcome.CHECKED,
            checked_at=NOW,
        )

    monkeypatch.setattr(chat_channel_router, "check_instance_membership", _fake_check)

    platform_result = await check_bot_membership(
        BotMembershipCheckRequest(instance_key="inst_b"),
        _current_user=_platform_owner(),
    )
    assert platform_result.outcome is MembershipCheckOutcome.CHECKED
    assert platform_result.instance_key == "inst_b"

    own_result = await check_bot_membership(
        BotMembershipCheckRequest(instance_key="inst_a"),
        _current_user=_tenant_admin(),
    )
    assert own_result.instance_key == "inst_a"

    with pytest.raises(NotFoundError):
        await check_bot_membership(
            BotMembershipCheckRequest(instance_key="inst_b"),
            _current_user=_tenant_admin(),
        )

    with pytest.raises(ValidationError):
        await check_bot_membership(
            BotMembershipCheckRequest(instance_key="wx_a", adapter_key="wechat_ilink_multi"),
            _current_user=_tenant_admin(),
        )


async def test_disable_preview_is_scoped(db) -> None:
    await _seed_routes()

    platform = await preview_disable_not_in_group(_current_user=_platform_owner())
    # a3 已停用、a4 没标记，都不该出现
    assert platform.count == 2
    assert {item.chat_key for item in platform.items} == {
        f"{ADAPTER}-inst_a-group_100001",
        f"{ADAPTER}-inst_b-group_200001",
    }

    tenant = await preview_disable_not_in_group(_current_user=_tenant_admin())
    assert tenant.count == 1
    assert tenant.items[0].chat_key == f"{ADAPTER}-inst_a-group_100001"
    assert tenant.items[0].custom_channel_name is None

    scoped = await preview_disable_not_in_group(instance_key="inst_a", _current_user=_tenant_admin())
    assert scoped.count == 1

    with pytest.raises(NotFoundError):
        await preview_disable_not_in_group(instance_key="inst_b", _current_user=_tenant_admin())


async def test_disable_execution_skips_everything_but_marked_and_active(db) -> None:
    seeded = await _seed_routes()
    a5 = await _make_channel("inst_a", "group_100005")
    await _mark(a5, BotMembershipStatus.NOT_IN_GROUP, checked_at=NOW)
    # 手工停用会清掉自动停用标记；先给 a5 留一个残留的自动停用标记
    data = _data(a5)
    data[CHANNEL_DATA_AUTO_DISABLE_KEY] = {
        "reason": "bot_left_group",
        "detail": "旧的自动停用",
        "disabled_at": NOW,
        "previous_status": "active",
    }
    a5.data = json.dumps(data, ensure_ascii=False)
    await a5.save(update_fields=["data"])

    chat_keys = [
        a5.chat_key,
        seeded.a2.chat_key,
        seeded.a3.chat_key,
        seeded.b1.chat_key,
        f"{ADAPTER}-inst_a-group_999999",
    ]
    result = await disable_not_in_group(
        BotMembershipDisableRequest(chat_keys=chat_keys),
        _current_user=_tenant_admin(),
    )

    assert result.disabled == 1
    assert result.skipped == 4
    assert result.disabled_chat_keys == [a5.chat_key]

    await a5.refresh_from_db()
    assert a5.is_active is False
    assert a5.get_auto_disable_info() is None
    info = a5.get_bot_membership_info()
    assert info is not None and info.status is BotMembershipStatus.NOT_IN_GROUP

    await seeded.a2.refresh_from_db()
    await seeded.a3.refresh_from_db()
    await seeded.b1.refresh_from_db()
    assert seeded.a2.is_active is True
    assert seeded.a3.is_active is False
    assert seeded.b1.is_active is True


async def test_disable_rejects_too_many_keys(db) -> None:
    with pytest.raises(ValidationError):
        await disable_not_in_group(
            BotMembershipDisableRequest(chat_keys=[f"onebot_v11-inst_a-group_{index}" for index in range(2001)]),
            _current_user=_platform_owner(),
        )


# ---------------------------------------------------------------------------
# 12. 调度
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "started_at", "last_run_at", "interval_hours", "expected"),
    [
        pytest.param(1000.0, 0.0, None, 0, False, id="disabled"),
        pytest.param(100.0, 0.0, None, 6, False, id="first-before-delay"),
        pytest.param(700.0, 0.0, None, 6, True, id="first-after-delay"),
        pytest.param(1000.0, 0.0, 900.0, 6, False, id="before-interval"),
        pytest.param(23000.0, 0.0, 900.0, 6, True, id="after-interval"),
    ],
)
def test_is_check_due(
    now: float,
    started_at: float,
    last_run_at: Optional[float],
    interval_hours: int,
    expected: bool,
) -> None:
    assert is_check_due(
        now=now,
        started_at=started_at,
        last_run_at=last_run_at,
        interval_hours=interval_hours,
    ) is expected


async def test_scheduler_start_and_stop_leave_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(membership_check, "check_all_instances", _noop_check)

    scheduler = BotMembershipScheduler()
    scheduler._tick_seconds = 0.01
    scheduler.start(lambda: 6)
    task = scheduler._task
    assert task is not None

    await scheduler.stop()

    assert scheduler._task is None
    assert task.done()


async def _noop_check() -> List[MembershipCheckResult]:
    return []


async def test_scheduler_runs_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []

    async def _fake_check() -> List[MembershipCheckResult]:
        calls.append(1)
        return []

    monkeypatch.setattr(membership_check, "check_all_instances", _fake_check)
    monkeypatch.setattr(membership_check, "INITIAL_DELAY_SECONDS", 0)

    scheduler = BotMembershipScheduler()
    scheduler._tick_seconds = 0.01
    scheduler.start(lambda: 1)
    await asyncio.sleep(0.06)
    await scheduler.stop()

    assert calls


async def test_scheduler_does_nothing_when_interval_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []

    async def _fake_check() -> List[MembershipCheckResult]:
        calls.append(1)
        return []

    monkeypatch.setattr(membership_check, "check_all_instances", _fake_check)
    monkeypatch.setattr(membership_check, "INITIAL_DELAY_SECONDS", 0)

    scheduler = BotMembershipScheduler()
    scheduler._tick_seconds = 0.01
    scheduler.start(lambda: 0)
    await asyncio.sleep(0.06)
    await scheduler.stop()

    assert calls == []


# ---------------------------------------------------------------------------
# 13. 配置默认值
# ---------------------------------------------------------------------------


def test_config_default_interval() -> None:
    from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Config

    assert OnebotV11Config().BOT_MEMBERSHIP_CHECK_INTERVAL_HOURS == 6


# ---------------------------------------------------------------------------
# 14. 整轮核对、调度器与适配器的衔接
# ---------------------------------------------------------------------------


async def test_check_all_instances_skips_inactive_instances_and_survives_errors(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_instance("inst_ok")
    await _make_instance("inst_boom", account_id=ACCOUNT_B)
    await _make_instance("inst_off", account_id="10003", enabled=False)
    await _make_instance("inst_arch", account_id="10004", status="archived")
    await _make_instance("inst_dis", account_id="10005", status="disabled")
    await _make_instance("tg_a", account_id="10006", adapter_key="telegram")

    called: List[str] = []

    async def _fake_check(instance_key: str, **_kwargs: Any) -> MembershipCheckResult:
        called.append(instance_key)
        if instance_key == "inst_boom":
            raise RuntimeError("这个实例出错")
        return MembershipCheckResult(instance_key=instance_key, outcome=MembershipCheckOutcome.CHECKED, checked_at=NOW)

    monkeypatch.setattr(membership_check, "check_instance_membership", _fake_check)

    results = await membership_check.check_all_instances()

    # 停用、归档、标成 disabled 的实例与别的适配器都不核对；一个实例出错不拦后面的
    assert sorted(called) == ["inst_boom", "inst_ok"]
    assert [result.instance_key for result in results] == ["inst_ok"]


async def test_check_does_not_overwrite_concurrent_data_edits(db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _make_instance("inst_a")
    channel = await _make_channel("inst_a", "group_100002", data=json.dumps({"custom_channel_name": "旧名"}))
    _install_bot(monkeypatch, FakeBot(_groups(100001)))

    async def _edit_meanwhile(chat_key: str, _since: int) -> bool:
        # 核对已经读出频道、还没写标记的这段时间里，管理员在后台改了自定义名称
        row = await DBChatChannel.filter(chat_key=chat_key).first()
        assert row is not None
        await row.set_custom_channel_name("新名")
        return False

    monkeypatch.setattr(membership_check, "has_inbound_message_since", _edit_meanwhile)

    result = await check_instance_membership("inst_a", now=NOW)

    assert result.outcome is MembershipCheckOutcome.CHECKED
    await channel.refresh_from_db()
    assert channel.get_custom_channel_name() == "新名"
    info = channel.get_bot_membership_info()
    assert info is not None and info.status is BotMembershipStatus.NOT_IN_GROUP


async def test_disable_uses_the_earliest_row_for_duplicate_chat_keys(db) -> None:
    await _make_instance("inst_a")
    first = await _make_channel("inst_a", "group_100008")
    # 存量残留：另一行带着同一个 chat_key（频道表没有对 chat_key 做唯一约束）
    second = await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key="inst_a",
        channel_id="group_100008_dup",
        chat_key=first.chat_key,
        channel_type="group",
        is_active=True,
        data="{}",
    )
    await _mark(first, BotMembershipStatus.NOT_IN_GROUP)
    await _mark(second, BotMembershipStatus.NOT_IN_GROUP)

    result = await disable_not_in_group(
        BotMembershipDisableRequest(chat_keys=[first.chat_key]),
        _current_user=_platform_owner(),
    )

    assert result.disabled == 1
    await first.refresh_from_db()
    await second.refresh_from_db()
    # 和取频道的惯例一致：停的是最早那行（消息链路用的就是它）
    assert first.is_active is False
    assert second.is_active is True


async def test_scheduler_counts_a_failed_round_as_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []

    async def _failing_check() -> List[MembershipCheckResult]:
        calls.append(1)
        raise RuntimeError("数据库抖了一下")

    monkeypatch.setattr(membership_check, "check_all_instances", _failing_check)
    monkeypatch.setattr(membership_check, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(membership_check, "logger", _LogRecorder())

    scheduler = BotMembershipScheduler()
    scheduler._tick_seconds = 0.01
    scheduler.start(lambda: 1)
    await asyncio.sleep(0.08)
    await scheduler.stop()

    # 失败的那一轮也记作跑过：间隔一小时，几个 tick 里只该跑一次，不该每个 tick 都重试
    assert calls == [1]


def test_adapter_init_starts_and_cleanup_stops_the_scheduler() -> None:
    """适配器 `init()` 启动调度器、`cleanup()` 停掉它

    `init()` 还会往 NoneBot 注册匹配器与钩子，不便在单元测试里真跑，所以按源码结构查。
    """
    import ast
    import inspect

    from nekro_agent.adapters.onebot_v11 import adapter as adapter_module

    tree = ast.parse(inspect.getsource(adapter_module.OnebotV11Adapter))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {"init", "cleanup"}
    }

    def _scheduler_calls(function: ast.AsyncFunctionDef) -> List[str]:
        return [
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "bot_membership_scheduler"
        ]

    assert _scheduler_calls(methods["init"]) == ["start"]
    assert _scheduler_calls(methods["cleanup"]) == ["stop"]
    assert any(
        isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
        for node in ast.walk(methods["cleanup"])
    ), "cleanup() 必须 await 调度器的 stop()"
