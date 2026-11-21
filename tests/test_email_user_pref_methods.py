"""三个邮件偏好方法：只改「正在说话的这个人自己的」设置

## 威胁模型

群里的模型没有「当前用户」可读。如果这三个方法接受一个用户 ID 参数（或者退回读
`ctx` 上的什么东西），群友就能：

- 让 Bot 把「给张三的节日祝福」改投到自己的邮箱（`set_my_mail_preference(email=...)`）；
- 替别人退订，或者把别人退订过的重新订阅回来（`festival=subscribe`）；
- 用 `forget_my_mail_preference` 抹掉别人留过的地址。

所以身份只有一条来源：宿主在消息到达时记下的「本轮发言人」（`session_speaker`）。
本文件逐条钉住「判据松一档就会发生的那件事」，以及方法**没有**用户参数这件事。

## 为什么也要钉「退订不能被删回来」

删一行偏好等于「没有偏好」；而「没有偏好」在节日名单里恰好等于「和本实例开过私聊的人
照样收」。于是「删一次偏好」就成了「悄悄重新订阅」。方法里对退订过的行只清地址与订阅、
保留退订标记——这条不是实现细节，是用户表达过的意愿。
"""

from __future__ import annotations

from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.agent import round_trigger_accounts as rta
from nekro_agent.services.ops_agent.triggers import RoundTrigger

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
TENANT_ID = 1

GROUP_CHAT = f"{ADAPTER}-{INSTANCE}-group_10001"
PRIVATE_CHAT = f"{ADAPTER}-{INSTANCE}-private_10001"
NO_INSTANCE_CHAT = "email-ops@example.test"

ALICE = "".join(("1234", "567"))
BOB = "".join(("7654", "321"))

ALICE_ADDRESS = "alice@example.test"


def ctx_of(chat_key: Optional[str]) -> AgentCtx:
    return AgentCtx(from_chat_key=chat_key, container_key="probe-container")


def speaker(chat_key: str, userid: str = ALICE) -> None:
    """把「这一轮是某个人在说话」布置好（宿主在消息到达时做的同一件事）"""
    rta.note_pending_trigger(chat_key, RoundTrigger(kind="human", sender_id=userid))
    rta.begin_round_triggers(chat_key)


@pytest.fixture(autouse=True)
def clean_round_state():
    rta.reset_round_triggers()
    rta.reset_clock()
    yield
    rta.reset_round_triggers()
    rta.reset_clock()


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
            tenant_id=TENANT_ID,
            metadata_json="",
        )
        for chat_key, channel_id, channel_type in (
            (GROUP_CHAT, "group_10001", "group"),
            (PRIVATE_CHAT, "private_10001", "private"),
        ):
            await DBChatChannel.create(
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                channel_id=channel_id,
                chat_key=chat_key,
                channel_type=channel_type,
                data="{}",
                is_active=True,
            )
        # 单账号适配器的频道：没有实例段
        await DBChatChannel.create(
            adapter_key="email",
            instance_key="",
            channel_id=NO_INSTANCE_CHAT,
            chat_key=NO_INSTANCE_CHAT,
            channel_type="private",
            data="{}",
        )
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


async def pref_of(userid: str = ALICE) -> Optional[DBEmailUserPref]:
    return await DBEmailUserPref.get_or_none(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        platform_userid=userid,
    )


class TestTheMethodsHaveNoUserParameter:
    """身份不是参数：多一个用户 ID 参数，群里就能替别人改"""

    def test_the_three_signatures_carry_no_user_or_account_argument(self) -> None:
        import inspect

        from plugins.builtin import email_utils

        for name in ("set_my_mail_preference", "get_my_mail_preference", "forget_my_mail_preference"):
            method = getattr(email_utils, name)
            parameters = {
                parameter
                for parameter in inspect.signature(method).parameters
                if parameter != "_ctx"
            }
            assert parameters <= {"email", "festival"}, f"{name} 多了参数: {parameters}"


class TestTheIdentityComesFromTheHost:
    async def test_a_group_round_with_one_speaker_writes_that_persons_row(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)

        assert result["success"] is True
        row = await pref_of()
        assert row is not None
        assert row.email == ALICE_ADDRESS
        assert row.set_in_chat_key == GROUP_CHAT

    async def test_two_speakers_in_one_round_are_refused(self, db) -> None:
        """这一轮有两个人时模型分不清该听谁的：一律拒绝并请对方单独再说一次"""
        from plugins.builtin import email_utils

        rta.note_pending_trigger(GROUP_CHAT, RoundTrigger(kind="human", sender_id=ALICE))
        rta.note_pending_trigger(GROUP_CHAT, RoundTrigger(kind="human", sender_id=BOB))
        rta.begin_round_triggers(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)

        assert result["success"] is False
        assert result["message"] == mail.SPEAKER_REQUIRED
        assert await pref_of() is None
        assert await pref_of(BOB) is None

    async def test_a_round_without_any_speaker_is_refused(self, db) -> None:
        from plugins.builtin import email_utils

        rta.begin_round_triggers(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="subscribe")

        assert result["success"] is False
        assert result["message"] == mail.SPEAKER_REQUIRED
        assert await pref_of() is None

    async def test_a_channel_without_an_instance_has_no_preference_to_set(self, db) -> None:
        """没有实例段就没有「这个实例的偏好」可存：按不支持处理，不退回平台方"""
        from plugins.builtin import email_utils

        speaker(NO_INSTANCE_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(NO_INSTANCE_CHAT), email=ALICE_ADDRESS)

        assert result["success"] is False
        assert result["message"] == mail.PREF_NOT_AVAILABLE
        assert await pref_of() is None

    async def test_the_message_of_get_is_the_same_refusal(self, db) -> None:
        from plugins.builtin import email_utils

        rta.begin_round_triggers(GROUP_CHAT)

        assert await email_utils.get_my_mail_preference(ctx_of(GROUP_CHAT)) == mail.SPEAKER_REQUIRED

    async def test_the_message_of_forget_is_the_same_refusal(self, db) -> None:
        from plugins.builtin import email_utils

        rta.begin_round_triggers(GROUP_CHAT)

        result = await email_utils.forget_my_mail_preference(ctx_of(GROUP_CHAT))

        assert result["success"] is False
        assert result["message"] == mail.SPEAKER_REQUIRED


class TestSettingTheAddress:
    async def test_a_bad_address_is_refused_and_nothing_is_written(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        for bad in ("", "no-at-sign", "a@b", "a b@example.test", "a@example.test, b@example.test"):
            result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=bad)
            if not bad.strip():
                # 空串是「清掉地址」，不是「填一个坏地址」
                assert result["success"] is True, bad
                continue
            assert result["success"] is False, bad
            assert result["message"] == mail.INVALID_MAIL_ADDRESS, bad

    async def test_an_empty_address_clears_the_saved_one(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email="  ")

        assert result["success"] is True
        row = await pref_of()
        assert row is not None
        assert row.email == ""

    async def test_the_confirmation_masks_the_address(self, db) -> None:
        """回执会落成 SYSTEM 消息：群里不该因为「确认一下」就把地址念出来"""
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)

        assert ALICE_ADDRESS not in result["message"]
        assert "a***@example.test" in result["message"]


class TestSubscribing:
    async def test_subscribe_sets_the_flag(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="subscribe")

        assert result["success"] is True
        row = await pref_of()
        assert row is not None
        assert row.festival_subscribed is True
        assert row.unsubscribed is False

    async def test_unsubscribe_keeps_the_address_but_stops_the_mail(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="unsubscribe")

        assert result["success"] is True
        row = await pref_of()
        assert row is not None
        assert row.email == ALICE_ADDRESS
        assert row.festival_subscribed is False
        assert row.unsubscribed is True
        assert row.unsubscribed_at is not None

    async def test_an_unknown_action_is_refused(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="maybe")

        assert result["success"] is False
        assert await pref_of() is None

    async def test_calling_with_neither_argument_changes_nothing(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        result = await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT))

        assert result["success"] is False
        assert await pref_of() is None


class TestReadingThePreference:
    async def test_a_private_chat_shows_the_whole_address(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(PRIVATE_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(PRIVATE_CHAT), email=ALICE_ADDRESS)

        text = await email_utils.get_my_mail_preference(ctx_of(PRIVATE_CHAT))

        assert ALICE_ADDRESS in text
        assert "没有订阅节日邮件" in text

    async def test_a_group_shows_only_the_masked_address(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)

        text = await email_utils.get_my_mail_preference(ctx_of(GROUP_CHAT))

        assert ALICE_ADDRESS not in text
        assert "a***@example.test" in text

    async def test_without_a_saved_address_the_default_one_is_named(self, db) -> None:
        """`onebot_v11` 的账号能拼出默认地址：说出来比「没设置」有用"""
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        text = await email_utils.get_my_mail_preference(ctx_of(GROUP_CHAT))

        assert "默认地址" in text
        assert "1***@qq.com" in text
        assert ALICE not in text.replace("1***@qq.com", ""), "群里的回显不该出现完整账号拼出的地址"

    async def test_the_subscribed_and_unsubscribed_states_are_told_apart(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="subscribe")
        assert "你订阅了节日邮件" in await email_utils.get_my_mail_preference(ctx_of(GROUP_CHAT))

        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="unsubscribe")
        assert "退订" in await email_utils.get_my_mail_preference(ctx_of(GROUP_CHAT))


class TestForgetting:
    async def test_a_plain_preference_row_is_deleted(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS, festival="subscribe")

        result = await email_utils.forget_my_mail_preference(ctx_of(GROUP_CHAT))

        assert result["success"] is True
        assert await pref_of() is None

    async def test_an_unsubscribed_person_stays_unsubscribed(self, db) -> None:
        """删一次偏好不能等于把他重新订阅回来"""
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), festival="unsubscribe")

        result = await email_utils.forget_my_mail_preference(ctx_of(GROUP_CHAT))

        assert result["success"] is True
        row = await pref_of()
        assert row is not None
        assert row.email == ""
        assert row.festival_subscribed is False
        assert row.unsubscribed is True

    async def test_forgetting_without_a_row_is_not_an_error(self, db) -> None:
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT)

        result = await email_utils.forget_my_mail_preference(ctx_of(GROUP_CHAT))

        assert result["success"] is True


class TestOnePersonDoesNotTouchAnother:
    async def test_a_second_speaker_writes_a_second_row(self, db) -> None:
        """同一个群里换一个人说话，写的是他自己那一行：别人的地址不该被覆盖"""
        from plugins.builtin import email_utils

        speaker(GROUP_CHAT, ALICE)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)
        rta.clear_round_triggers(GROUP_CHAT)

        speaker(GROUP_CHAT, BOB)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email="bob@example.test")

        alice_row = await pref_of(ALICE)
        bob_row = await pref_of(BOB)
        assert alice_row is not None and alice_row.email == ALICE_ADDRESS
        assert bob_row is not None and bob_row.email == "bob@example.test"

    async def test_the_same_person_in_two_instances_has_two_rows(self, db) -> None:
        """偏好按（适配器, 实例, 账号）一行：同一个人在另一个租户的 Bot 眼里是另一个身份"""
        from plugins.builtin import email_utils

        other_chat = f"{ADAPTER}-rival1-group_20001"
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="rival1",
            display_name="rival1",
            status="online",
            provider="remote_onebot",
            tenant_id=2,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key="rival1",
            channel_id="group_20001",
            chat_key=other_chat,
            channel_type="group",
            data="{}",
            is_active=True,
        )

        speaker(GROUP_CHAT, ALICE)
        await email_utils.set_my_mail_preference(ctx_of(GROUP_CHAT), email=ALICE_ADDRESS)
        rta.clear_round_triggers(GROUP_CHAT)

        speaker(other_chat, ALICE)
        await email_utils.set_my_mail_preference(ctx_of(other_chat), email="alice@other.example.test")

        rows = await DBEmailUserPref.filter(platform_userid=ALICE).order_by("id")
        assert [(row.instance_key, row.email) for row in rows] == [
            (INSTANCE, ALICE_ADDRESS),
            ("rival1", "alice@other.example.test"),
        ]
