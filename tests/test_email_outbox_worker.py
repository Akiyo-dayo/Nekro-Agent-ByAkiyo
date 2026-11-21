"""节日邮件队列：名单、入队去重、发出前重判、匀速、重试、清理与后台管理

## 这里钉什么

队列里有两条**判据会过期**，它们才是这个文件的主体：

1. **名单是入队那一刻算的**。入队到发出之间可能过去很久（积压、暂停后恢复、进程重启），
   这期间用户可能退订、平台方可能取消共享、实例可能被停用、总闸可能被关掉。所以每一封
   发出前都把判据重跑一遍，任何一条不过就记 `skipped` 并清空正文——不换发件人（换一个
   等于绕过了平台方「这个邮箱不给这个实例用」的决定），不补发，也不悄悄丢掉。

2. **一天只发一封**。入队时按发信记录去重，发出前再按同一条判据查一次：入队与发出之间
   正好有一封被工人发掉了，第二条路也会被挡住。

其余几条是「别把它做成垃圾邮件机器」：同一个发件邮箱两封之间至少隔配置的秒数、失败按
1 分钟 / 5 分钟重试两次、结束的行 7 天后清掉、正文在结束时就清空。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.core.config import config as core_config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_outbox import DBEmailOutbox
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.services import email_accounts as mail
from nekro_agent.services import email_outbox

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER_INSTANCE = "inst2"

TENANT_ID = 1
OTHER_TENANT_ID = 2

# 5~8 位短号：够用来当账号，又不构成 9~11 位的号码形态
ALICE = "".join(("1234", "567"))
BOB = "".join(("7654", "321"))

# `onebot_v11` 的私聊频道名就是对方账号（`private_<QQ>`）：频道名里的号要与下面那两个一致
PRIVATE_CHAT = f"{ADAPTER}-{INSTANCE}-private_{ALICE}"
GROUP_CHAT = f"{ADAPTER}-{INSTANCE}-group_20001"
OTHER_PRIVATE_CHAT = f"{ADAPTER}-{OTHER_INSTANCE}-private_54321"

ALICE_ADDRESS = "alice@example.test"
BOB_ADDRESS = "bob@example.test"

FESTIVAL = "中秋"
DATE = "2024-09-17"


def sender_view() -> Any:
    """一个平台方发件人视图（队列只按 kind + ref 找它对不对得上）"""
    return mail.build_account_view(
        kind=mail.KIND_PLATFORM,
        username="ops@example.test",
        display_name="通知",
        send_enabled=True,
        readable=True,
    )


def other_sender_view() -> Any:
    """同一个实例里的第二个可发件账户：用来证明匀速是按发件邮箱算的"""
    return mail.build_account_view(
        kind=mail.KIND_PLATFORM,
        username="notice@example.test",
        display_name="公告",
        send_enabled=True,
        readable=True,
    )


def recipient(
    userid: str = ALICE,
    address: str = ALICE_ADDRESS,
    *,
    nickname: str = "小明",
    source: str = "private",
) -> Any:
    return email_outbox.FestivalRecipient(
        platform_userid=userid,
        to_address=address,
        nickname=nickname,
        source=source,
    )


@pytest.fixture(autouse=True)
def clean_state():
    email_outbox.reset_outbox_state()
    mail.reset_send_log_state()
    mail.clear_default_sender_cache()
    yield
    email_outbox.reset_outbox_state()
    mail.reset_send_log_state()
    mail.clear_default_sender_cache()


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in ((INSTANCE, TENANT_ID), (OTHER_INSTANCE, OTHER_TENANT_ID)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        for chat_key, channel_type in (
            (PRIVATE_CHAT, "private"),
            (GROUP_CHAT, "group"),
            (OTHER_PRIVATE_CHAT, "private"),
        ):
            channel_id = chat_key.rsplit("-", maxsplit=1)[-1]
            await DBChatChannel.create(
                adapter_key=ADAPTER,
                instance_key=chat_key.split("-")[1],
                channel_id=channel_id,
                chat_key=chat_key,
                channel_type=channel_type,
                data="{}",
                is_active=True,
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def plugins_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个插件在实例与频道上都生效（默认就是全开，这里只是把它写明）"""
    monkeypatch.setattr(email_outbox, "plugin_effective_on_instance", lambda *_a, **_k: True)
    monkeypatch.setattr(email_outbox, "plugin_effective_with_instance", lambda *_a, **_k: True)
    monkeypatch.setattr(core_config, "EMAIL_FESTIVAL_MAIL_ENABLED", True)
    monkeypatch.setattr(core_config, "EMAIL_OUTBOX_INTERVAL_SECONDS", 1)


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch, plugins_on) -> dict:
    """把发件人算成固定的一份，并把真正发信换成记录调用的替身"""
    seen: dict[str, Any] = {"sent": [], "fail": 0, "sendable": [sender_view(), other_sender_view()]}

    async def default_sender(_adapter_key: str, _instance_key: str, **_kwargs: Any) -> Any:
        return seen["sendable"][0]

    async def instance_accounts(_adapter_key: str, _instance_key: str, **_kwargs: Any) -> Any:
        return mail.SessionAccounts(
            resolved=True,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            is_platform_instance=False,
            readable=list(seen["sendable"]),
            sendable=list(seen["sendable"]),
        )

    async def send_via_account(view: Any, *, to_address: str, subject: str, content: str) -> None:
        seen["sent"].append({"to": to_address, "subject": subject, "content": content, "account": view.username})
        if seen["fail"] > 0:
            seen["fail"] -= 1
            raise RuntimeError("smtp said no")

    monkeypatch.setattr(email_outbox.mail, "default_sender_for_instance", default_sender)
    monkeypatch.setattr(email_outbox.mail, "instance_accounts", instance_accounts)
    monkeypatch.setattr(email_outbox.mail, "send_via_account", send_via_account)
    return seen


async def add_instance_account(instance_key: str = INSTANCE) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username="own@example.test",
        display_name="本实例",
        provider="自定义",
        settings={},
        password="secret",
        enabled=True,
        receive_enabled=True,
        send_enabled=True,
        is_default_sender=False,
        attachment_dir="_i1",
    )


async def enqueue_one(**overrides: Any) -> int:
    values: dict[str, Any] = {
        "adapter_key": ADAPTER,
        "instance_key": INSTANCE,
        "festival_name": FESTIVAL,
        "date_str": DATE,
        "sender": sender_view(),
        "recipients": [recipient()],
        "subject": "中秋快乐",
        "body": "祝你团圆。",
    }
    values.update(overrides)
    return await email_outbox.enqueue_festival_batch(**values)


async def add_message(chat_key: str, userid: str, *, nickname: str = "小明", bot: bool = False) -> DBChatMessage:
    adapter_key, instance_key, channel_id = chat_key.split("-")
    return await DBChatMessage.create(
        sender_id="-1" if bot else userid,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=adapter_key,
        instance_key=instance_key,
        message_id=f"{chat_key}-{userid}-{db_now().timestamp()}",
        chat_key=chat_key,
        chat_type="private" if channel_id.startswith("private") else "group",
        platform_userid=userid,
        content_text="在吗",
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=int(db_now().timestamp()),
    )


class TestTheRecipientList:
    async def test_a_private_chat_partner_is_a_recipient(self, db, plugins_on) -> None:
        await add_message(PRIVATE_CHAT, ALICE)

        recipients = await email_outbox.festival_recipients(ADAPTER, INSTANCE)

        assert [(item.platform_userid, item.to_address, item.nickname, item.source) for item in recipients] == [
            (ALICE, f"{ALICE}@qq.com", "小明", "private"),
        ]

    async def test_a_group_subscriber_is_a_recipient(self, db, plugins_on) -> None:
        await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid=BOB,
            email=BOB_ADDRESS,
            festival_subscribed=True,
            set_in_chat_key=GROUP_CHAT,
        )

        recipients = await email_outbox.festival_recipients(ADAPTER, INSTANCE)

        subscribed = [item for item in recipients if item.platform_userid == BOB]
        assert [(item.to_address, item.source) for item in subscribed] == [(BOB_ADDRESS, "subscribed")]

    async def test_an_unsubscribed_person_is_dropped_even_from_a_private_chat(self, db, plugins_on) -> None:
        await add_message(PRIVATE_CHAT, ALICE)

        await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid=ALICE,
            unsubscribed=True,
            unsubscribed_at=db_now(),
        )

        assert await email_outbox.festival_recipients(ADAPTER, INSTANCE) == []

    async def test_a_disabled_channel_is_not_a_recipient(self, db, plugins_on) -> None:
        """停掉的会话等于「不想让 Bot 再往那里说话」，也不该再收到节日邮件"""
        await add_message(PRIVATE_CHAT, ALICE)

        await DBChatChannel.filter(chat_key=PRIVATE_CHAT).update(is_active=False)

        assert await email_outbox.festival_recipients(ADAPTER, INSTANCE) == []

    async def test_a_plugin_that_is_off_on_the_chat_drops_the_recipient(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """只开了节日祝福、没开邮箱工具时发不了信：这类人也不该进名单"""
        await add_message(PRIVATE_CHAT, ALICE)

        monkeypatch.setattr(email_outbox, "plugin_effective_with_instance", lambda key, *_a, **_k: key == email_outbox.FESTIVAL_PLUGIN_KEY)

        assert await email_outbox.festival_recipients(ADAPTER, INSTANCE) == []

    async def test_two_channels_of_the_same_person_yield_one_recipient(self, db, plugins_on) -> None:
        await add_message(PRIVATE_CHAT, ALICE)

        await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid=ALICE,
            email=ALICE_ADDRESS,
            festival_subscribed=True,
        )

        recipients = await email_outbox.festival_recipients(ADAPTER, INSTANCE)

        assert len(recipients) == 1
        assert recipients[0].to_address == ALICE_ADDRESS, "有偏好地址时优先用它"

    async def test_an_unsupported_adapter_has_no_default_address(self, db, plugins_on) -> None:
        """不是 `onebot_v11` 就拼不出默认地址：没有留地址的人跳过，不猜"""
        assert mail.default_festival_address("qqbot_openclaw", ALICE) == ""

    async def test_the_nickname_is_cleaned_and_capped(self, db, plugins_on) -> None:
        assert email_outbox.clean_nickname("小\n明\u0000" + "x" * 40) == ("小明" + "x" * 40)[: email_outbox.NICKNAME_MAX_CHARS]
        assert email_outbox.clean_nickname("   ") == email_outbox.NICKNAME_FALLBACK


class TestTheReadyCheck:
    async def test_a_healthy_instance_has_a_sender(self, db, plugins_on, monkeypatch: pytest.MonkeyPatch) -> None:
        await add_instance_account()

        async def default_sender(*_args: Any, **_kwargs: Any) -> Any:
            return sender_view()

        monkeypatch.setattr(email_outbox.mail, "default_sender_for_instance", default_sender)

        assert await email_outbox.instance_festival_mail_ready(ADAPTER, INSTANCE) is not None

    async def test_the_global_switch_off_means_not_ready(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(email_outbox, "plugin_effective_on_instance", lambda *_a, **_k: True)
        monkeypatch.setattr(core_config, "EMAIL_FESTIVAL_MAIL_ENABLED", False)

        assert await email_outbox.instance_festival_mail_ready(ADAPTER, INSTANCE) is None

    @pytest.mark.parametrize("off", ["festival", "email_utils"])
    async def test_one_plugin_off_means_not_ready(self, db, monkeypatch: pytest.MonkeyPatch, off: str) -> None:
        """两个插件缺一个都不行；发件人本来是有的，挡住它的只能是插件判据"""
        await add_instance_account()

        async def default_sender(*_args: Any, **_kwargs: Any) -> Any:
            return sender_view()

        monkeypatch.setattr(email_outbox.mail, "default_sender_for_instance", default_sender)
        monkeypatch.setattr(core_config, "EMAIL_FESTIVAL_MAIL_ENABLED", True)
        disabled = email_outbox.FESTIVAL_PLUGIN_KEY if off == "festival" else email_outbox.EMAIL_UTILS_PLUGIN_KEY
        monkeypatch.setattr(
            email_outbox,
            "plugin_effective_on_instance",
            lambda key, *_a, **_k: key != disabled,
        )

        assert await email_outbox.instance_festival_mail_ready(ADAPTER, INSTANCE) is None

    async def test_an_inactive_instance_is_not_ready(self, db, plugins_on) -> None:
        await DBAdapterInstance.filter(instance_key=INSTANCE).update(status="disabled")

        assert await email_outbox.instance_festival_mail_ready(ADAPTER, INSTANCE) is None

    async def test_no_sender_means_not_ready(self, db, plugins_on, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_sender(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(email_outbox.mail, "default_sender_for_instance", no_sender)

        assert await email_outbox.instance_festival_mail_ready(ADAPTER, INSTANCE) is None


class TestTheBatchKey:
    def test_it_is_short_and_stable(self) -> None:
        key = email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, INSTANCE)

        assert len(key) <= 64
        assert key == email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, INSTANCE)

    def test_two_instances_get_two_batches(self) -> None:
        first = email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, INSTANCE)
        second = email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, OTHER_INSTANCE)

        assert first != second

    def test_a_long_festival_name_is_clipped(self) -> None:
        key = email_outbox.festival_batch_key("节" * 100, DATE, ADAPTER, INSTANCE)

        assert len(key) <= 64


class TestEnqueueing:
    async def test_one_row_per_recipient_with_the_nickname_in_the_body(self, db, sender) -> None:
        queued = await enqueue_one()

        assert queued == 1
        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_QUEUED
        assert row.to_address == ALICE_ADDRESS
        assert row.body == "小明：\n\n祝你团圆。"
        assert row.reason == f"节日祝福：{FESTIVAL}；收件人在本实例开过私聊"
        assert row.account_kind == mail.KIND_PLATFORM
        assert row.account_ref == mail.account_send_log_ref(sender_view())

    async def test_a_group_subscriber_gets_the_other_reason(self, db, sender) -> None:
        await enqueue_one(recipients=[recipient(source="subscribed")])

        row = await DBEmailOutbox.get()
        assert row.reason == f"节日祝福：{FESTIVAL}；收件人在群里订阅过"

    async def test_the_same_batch_does_not_enqueue_the_same_address_twice(self, db, sender) -> None:
        assert await enqueue_one() == 1
        assert await enqueue_one() == 0
        assert await DBEmailOutbox.all().count() == 1

    async def test_an_address_that_got_a_festival_mail_today_is_not_enqueued_again(self, db, sender) -> None:
        await DBEmailSendLog.create(
            account_kind=mail.KIND_PLATFORM,
            account_ref="abc",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            purpose=mail.PURPOSE_FESTIVAL,
            to_address=ALICE_ADDRESS,
            ok=True,
        )

        assert await enqueue_one() == 0

    async def test_a_failed_send_earlier_today_does_not_block_enqueueing(self, db, sender) -> None:
        await DBEmailSendLog.create(
            account_kind=mail.KIND_PLATFORM,
            account_ref="abc",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            purpose=mail.PURPOSE_FESTIVAL,
            to_address=ALICE_ADDRESS,
            ok=False,
        )

        assert await enqueue_one() == 1

    async def test_a_normal_mail_does_not_block_enqueueing(self, db, sender) -> None:
        await DBEmailSendLog.create(
            account_kind=mail.KIND_PLATFORM,
            account_ref="abc",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            purpose=mail.PURPOSE_SEND_EMAIL,
            to_address=ALICE_ADDRESS,
            ok=True,
        )

        assert await enqueue_one() == 1

    async def test_an_empty_list_enqueues_nothing(self, db, sender) -> None:
        assert await enqueue_one(recipients=[]) == 0
        assert await DBEmailOutbox.all().count() == 0

    async def test_the_subject_is_clipped(self, db, sender) -> None:
        await enqueue_one(subject="标" * 400)

        row = await DBEmailOutbox.get()
        assert len(row.subject) == 256


class TestProcessing:
    async def test_a_due_row_is_sent_and_logged(self, db, sender) -> None:
        await enqueue_one()

        assert await email_outbox.process_due_outbox() == 1

        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_SENT
        assert row.body == "", "发完清空正文"
        assert row.next_at is None
        log = await DBEmailSendLog.get()
        assert log.ok is True
        assert log.purpose == mail.PURPOSE_FESTIVAL
        assert log.tenant_id == TENANT_ID
        assert log.chat_key == ""
        assert sender["sent"][0]["to"] == ALICE_ADDRESS

    async def test_a_future_row_is_left_alone(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailOutbox.all().update(next_at=db_now() + timedelta(hours=1))

        assert await email_outbox.process_due_outbox() == 0
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED

    async def test_a_row_without_next_at_is_due_immediately(self, db, sender) -> None:
        """`next_at` 为空表示立即可发：只按 `next_at <= now` 取会让这一行永远留在队列里"""
        await enqueue_one()
        await DBEmailOutbox.all().update(next_at=None)

        assert await email_outbox.process_due_outbox() == 1
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SENT

    async def test_a_paused_row_is_left_alone(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailOutbox.all().update(status=email_outbox.STATUS_PAUSED)

        assert await email_outbox.process_due_outbox() == 0
        assert sender["sent"] == []

    async def test_one_sender_mailbox_sends_one_mail_per_round(self, db, sender, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一个发件邮箱两封之间至少隔配置的秒数：一轮里最多一封"""
        await enqueue_one(recipients=[recipient(ALICE, ALICE_ADDRESS), recipient(BOB, BOB_ADDRESS)])
        monkeypatch.setattr(core_config, "EMAIL_OUTBOX_INTERVAL_SECONDS", 600)

        assert await email_outbox.process_due_outbox() == 1
        assert len(sender["sent"]) == 1

    async def test_the_next_round_waits_for_the_interval(self, db, sender, monkeypatch: pytest.MonkeyPatch) -> None:
        """下一轮紧接着来也不发第二封；时钟走过间隔之后才发"""
        clock = {"now": 1000.0}
        email_outbox.set_clock(lambda: clock["now"])
        try:
            await enqueue_one(recipients=[recipient(ALICE, ALICE_ADDRESS), recipient(BOB, BOB_ADDRESS)])
            monkeypatch.setattr(core_config, "EMAIL_OUTBOX_INTERVAL_SECONDS", 5)

            assert await email_outbox.process_due_outbox() == 1
            clock["now"] += 4
            assert await email_outbox.process_due_outbox() == 0
            assert len(sender["sent"]) == 1
            clock["now"] += 1
            assert await email_outbox.process_due_outbox() == 1
            assert len(sender["sent"]) == 2
        finally:
            email_outbox.set_clock(None)

    async def test_the_interval_is_per_sender_mailbox(self, db, sender, monkeypatch: pytest.MonkeyPatch) -> None:
        """换一个发件邮箱就不受上一个的间隔约束：不同账户各走各的"""
        await enqueue_one(recipients=[recipient(ALICE, ALICE_ADDRESS)])
        await enqueue_one(sender=other_sender_view(), recipients=[recipient(BOB, BOB_ADDRESS)])
        monkeypatch.setattr(core_config, "EMAIL_OUTBOX_INTERVAL_SECONDS", 600)

        assert await email_outbox.process_due_outbox() == 2
        assert len(sender["sent"]) == 2
        assert {row.status for row in await DBEmailOutbox.all()} == {email_outbox.STATUS_SENT}


class TestTheChecksAreRedoneBeforeSending:
    async def test_the_global_switch_off_skips(self, db, sender, monkeypatch: pytest.MonkeyPatch) -> None:
        await enqueue_one()
        monkeypatch.setattr(core_config, "EMAIL_FESTIVAL_MAIL_ENABLED", False)

        await email_outbox.process_due_outbox()

        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_SKIPPED
        assert row.body == ""
        assert sender["sent"] == []
        assert await DBEmailSendLog.all().count() == 0, "没发出去就没有发信记录"

    async def test_an_inactive_instance_skips(self, db, sender) -> None:
        await enqueue_one()
        await DBAdapterInstance.filter(instance_key=INSTANCE).update(status="disabled")

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SKIPPED
        assert sender["sent"] == []

    async def test_a_plugin_switched_off_skips(self, db, sender, monkeypatch: pytest.MonkeyPatch) -> None:
        await enqueue_one()
        monkeypatch.setattr(email_outbox, "plugin_effective_on_instance", lambda *_a, **_k: False)

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SKIPPED
        assert sender["sent"] == []

    async def test_an_unsubscribe_after_enqueueing_skips(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid=ALICE,
            unsubscribed=True,
            unsubscribed_at=db_now(),
        )

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SKIPPED
        assert sender["sent"] == []

    async def test_a_mail_sent_today_by_another_path_skips(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailSendLog.create(
            account_kind=mail.KIND_PLATFORM,
            account_ref="abc",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            purpose=mail.PURPOSE_FESTIVAL,
            to_address=ALICE_ADDRESS,
            ok=True,
        )

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SKIPPED
        assert sender["sent"] == []

    async def test_a_sender_that_lost_the_share_skips_and_does_not_switch(
        self,
        db,
        sender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """换一个发件人发出去等于绕过了平台方「这个邮箱不给这个实例用」的决定"""
        await enqueue_one()

        async def no_accounts(_adapter_key: str, _instance_key: str, **_kwargs: Any) -> Any:
            return mail.SessionAccounts(
                resolved=True,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                is_platform_instance=False,
            )

        monkeypatch.setattr(email_outbox.mail, "instance_accounts", no_accounts)

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SKIPPED
        assert sender["sent"] == []


class TestRetrying:
    async def test_the_first_failure_waits_a_minute(self, db, sender) -> None:
        await enqueue_one()
        sender["fail"] = 1
        before = db_now()

        await email_outbox.process_due_outbox()

        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_QUEUED
        assert row.attempts == 1
        assert row.next_at is not None
        assert row.next_at >= before + timedelta(seconds=email_outbox.RETRY_DELAYS_SECONDS[0] - 5)

    async def test_the_second_failure_waits_five_minutes(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailOutbox.all().update(attempts=1)
        sender["fail"] = 1

        await email_outbox.process_due_outbox()

        row = await DBEmailOutbox.get()
        assert row.attempts == 2
        assert row.next_at is not None
        assert row.next_at >= db_now() + timedelta(seconds=email_outbox.RETRY_DELAYS_SECONDS[1] - 5)

    async def test_the_third_failure_is_final_and_logged(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailOutbox.all().update(attempts=email_outbox.MAX_ATTEMPTS - 1)
        sender["fail"] = 1

        await email_outbox.process_due_outbox()

        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_FAILED
        assert row.body == ""
        assert row.next_at is None
        log = await DBEmailSendLog.get()
        assert log.ok is False
        assert "smtp said no" in log.error

    async def test_a_success_after_a_failure_is_sent(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailOutbox.all().update(attempts=1, next_at=db_now() - timedelta(seconds=1))

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SENT

    async def test_an_own_mailbox_final_failure_is_logged_in_general_terms(self, db, sender, monkeypatch) -> None:
        """实例自有邮箱的记录租户看得到：原始异常（可能带对端地址）只进日志"""
        own = mail.build_account_view(
            kind=mail.KIND_INSTANCE,
            username="own@example.test",
            display_name="本实例",
            send_enabled=True,
            readable=True,
            instance_account_id=1,
        )
        sender["sendable"] = [own]
        internal = ".".join(("10", "0", "0", "5"))

        async def refused(*_args: Any, **_kwargs: Any) -> None:
            raise ConnectionRefusedError(f"{internal}:25 refused")

        monkeypatch.setattr(email_outbox.mail, "send_via_account", refused)
        await enqueue_one(sender=own)
        await DBEmailOutbox.all().update(attempts=email_outbox.MAX_ATTEMPTS - 1)

        await email_outbox.process_due_outbox()

        log = await DBEmailSendLog.get()
        assert log.error == mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT]
        assert internal not in log.error


class TestTheSenderReferenceSurvivesARestart:
    async def test_a_new_jwt_key_does_not_strand_queued_platform_mail(self, db, sender, monkeypatch, tmp_path) -> None:
        """引用存在队列里：密钥要是随 JWT 密钥变，重启之后整批会被判成「发件账户已不可用」"""
        from nekro_agent.core.os_env import OsEnv

        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        mail.reset_account_ref_key_cache()
        try:
            await enqueue_one()
            monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "secret:rotated-for-the-test")
            mail.reset_account_ref_key_cache()  # 等于重启

            await email_outbox.process_due_outbox()

            assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SENT
        finally:
            mail.reset_account_ref_key_cache()

    async def test_a_row_queued_before_the_upgrade_still_sends(self, db, sender, monkeypatch, tmp_path) -> None:
        """升级前排进队列的平台方邮件存的是从 JWT 密钥派生的旧版引用：照样认，不整批判成「发件账户已不可用」"""
        from nekro_agent.core.os_env import OsEnv

        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "secret:fixed-for-the-test")
        mail.reset_account_ref_key_cache()
        try:
            await enqueue_one()
            legacy = mail.legacy_send_log_refs(sender_view())[0]
            assert legacy != mail.account_send_log_ref(sender_view()), "前提：新旧两版引用不同"
            await DBEmailOutbox.all().update(account_ref=legacy)

            await email_outbox.process_due_outbox()

            assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SENT
            assert len(sender["sent"]) == 1
        finally:
            mail.reset_account_ref_key_cache()

    async def test_a_reference_of_no_known_account_is_still_skipped(self, db, sender) -> None:
        await enqueue_one()
        await DBEmailOutbox.all().update(account_ref="0123456789abcdef")

        await email_outbox.process_due_outbox()

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SKIPPED
        assert sender["sent"] == []


class TestThePurge:
    async def test_finished_rows_older_than_the_keep_window_are_deleted(self, db, sender) -> None:
        await enqueue_one(recipients=[recipient(ALICE, ALICE_ADDRESS), recipient(BOB, BOB_ADDRESS)])
        rows = await DBEmailOutbox.all().order_by("id")
        await DBEmailOutbox.filter(id=rows[0].id).update(
            status=email_outbox.STATUS_SENT,
            body="",
            update_time=db_now() - timedelta(days=email_outbox.OUTBOX_KEEP_DAYS + 1),
        )

        await email_outbox.process_due_outbox()

        assert await DBEmailOutbox.filter(status=email_outbox.STATUS_SENT).count() == 1, "老的那行被清掉"
        assert await DBEmailOutbox.all().count() == 1, "这一轮新发的那行还在"

    async def test_a_queued_row_is_never_purged(self, db, sender) -> None:
        await enqueue_one(recipients=[recipient(ALICE, ALICE_ADDRESS), recipient(BOB, BOB_ADDRESS)])
        rows = await DBEmailOutbox.all().order_by("id")
        await DBEmailOutbox.filter(id=rows[0].id).update(update_time=db_now() - timedelta(days=30))

        await email_outbox.process_due_outbox()

        assert await DBEmailOutbox.all().count() == 2

    async def test_the_purge_runs_at_most_once_a_day(self, db, sender) -> None:
        """清理是顺手做的：每一轮都删一遍会把发信拖慢，而积压的行本来就不多"""
        now = 1000.0
        email_outbox.set_clock(lambda: now)
        await enqueue_one()
        await email_outbox.process_due_outbox()
        await enqueue_one(recipients=[recipient(BOB, BOB_ADDRESS)], festival_name="国庆")
        await DBEmailOutbox.filter(status=email_outbox.STATUS_SENT).update(
            update_time=db_now() - timedelta(days=email_outbox.OUTBOX_KEEP_DAYS + 1),
        )

        now += 60
        assert await email_outbox.process_due_outbox() == 1

        assert await DBEmailOutbox.filter(status=email_outbox.STATUS_SENT).count() == 2, "不到一天不清理"


class TestTheBatchManagement:
    async def test_listing_counts_each_status(self, db, sender) -> None:
        await enqueue_one(recipients=[recipient(ALICE, ALICE_ADDRESS), recipient(BOB, BOB_ADDRESS)])
        first = await DBEmailOutbox.all().order_by("id").first()
        assert first is not None
        await DBEmailOutbox.filter(id=first.id).update(
            status=email_outbox.STATUS_SENT,
            update_time=db_now(),
        )

        batches = await email_outbox.list_batches(ADAPTER, INSTANCE)

        assert len(batches) == 1
        entry = batches[0]
        assert entry["festival_name"] == FESTIVAL
        assert entry["date"] == DATE
        assert entry["counts"][email_outbox.STATUS_SENT] == 1
        assert entry["counts"][email_outbox.STATUS_QUEUED] == 1

    async def test_pause_resume_and_void(self, db, sender) -> None:
        await enqueue_one()
        key = email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, INSTANCE)

        assert await email_outbox.pause_batch(ADAPTER, INSTANCE, key) == 1
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_PAUSED
        assert await email_outbox.pause_batch(ADAPTER, INSTANCE, key) == 0

        assert await email_outbox.resume_batch(ADAPTER, INSTANCE, key) == 1
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED

        assert await email_outbox.void_batch(ADAPTER, INSTANCE, key) == 1
        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_SKIPPED
        assert row.body == ""

    async def test_void_does_not_touch_a_sent_row(self, db, sender) -> None:
        await enqueue_one()
        key = email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, INSTANCE)
        await DBEmailOutbox.all().update(status=email_outbox.STATUS_SENT)

        assert await email_outbox.void_batch(ADAPTER, INSTANCE, key) == 0
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_SENT

    async def test_another_instances_batch_is_not_reachable(self, db, sender) -> None:
        await enqueue_one()
        key = email_outbox.festival_batch_key(FESTIVAL, DATE, ADAPTER, INSTANCE)

        assert await email_outbox.pause_batch(ADAPTER, OTHER_INSTANCE, key) == 0
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED


class TestTheWorkerLoop:
    async def test_start_and_stop_are_idempotent(self) -> None:
        email_outbox.start_outbox_worker()
        email_outbox.start_outbox_worker()
        await email_outbox.stop_outbox_worker()
        await email_outbox.stop_outbox_worker()

    async def test_the_loop_survives_a_failing_round(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """一轮失败不能让循环退出：明天也不会自己好"""
        calls = {"n": 0}

        async def flaky(**_kwargs: Any) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return 0

        monkeypatch.setattr(email_outbox, "process_due_outbox", flaky)
        monkeypatch.setattr(email_outbox, "WORKER_TICK_SECONDS", 0.01)
        monkeypatch.setattr(email_outbox, "WORKER_STARTUP_DELAY_SECONDS", 0.01)

        email_outbox.start_outbox_worker()
        try:
            for _ in range(200):
                if calls["n"] >= 2:
                    break
                import asyncio

                await asyncio.sleep(0.01)
        finally:
            await email_outbox.stop_outbox_worker()

        assert calls["n"] >= 2
