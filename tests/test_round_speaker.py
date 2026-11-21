"""本轮发言人的判定：只有「全是人、恰好一个、批次完整、没过期」才给名字

## 为什么要这么严

`set_my_mail_preference` / `forget_my_mail_preference` 改的是**正在说话的这个人自己的**设置。
宿主没有「当前用户」这种东西——会话是群，说话的人只从触发记录里能看出来。判据松一档的后果：

- 有定时器 / 系统推送时也认发言人 → 模型替一个根本没开口的人改了设置；
- 有两个人的触发时也认 → 群友诱导模型「把刚才那位朋友的祝福改投到我邮箱」；
- 攒的条目超上限被丢过还认 → 丢掉的可能正是另一个人，「只剩一个」是假象；
- 记录不过期 → 回合卡住后上一轮的人继续被当成这一轮的发言人。

所以本文件逐条钉住这四种「不给名字」，以及「给了名字」的那一种，外加两份记录互不干扰。
"""

import ast
from pathlib import Path

import pytest

from nekro_agent.services.agent import round_trigger_accounts as rta
from nekro_agent.services.ops_agent.triggers import RoundTrigger

CHAT = "onebot_v11-inst1-group_10001"
OTHER = "onebot_v11-inst1-group_10002"

# 5~8 位短号：够用来当账号，又不构成 9~11 位的号码形态
ALICE = "".join(("1234", "567"))
BOB = "".join(("7654", "321"))


def human(sender_id: str) -> RoundTrigger:
    return RoundTrigger(kind="human", sender_id=sender_id)


def timer() -> RoundTrigger:
    return RoundTrigger(kind="timer", sender_id="")


@pytest.fixture(autouse=True)
def clean_state():
    rta.reset_round_triggers()
    rta.reset_clock()
    yield
    rta.reset_round_triggers()
    rta.reset_clock()


class TestTheHappyPath:
    def test_one_human_trigger_becomes_the_speaker(self):
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) == ALICE

    def test_the_same_person_triggering_twice_is_still_one_speaker(self):
        """防抖会把同一个人的多条消息并成一轮：去重之后仍然只有一个人"""
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) == ALICE

    def test_a_trigger_arriving_after_the_round_started_does_not_change_it(self):
        """回合跑起来之后到达的触发属于下一轮，不该把这一轮的发言人换掉"""
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)
        rta.note_pending_trigger(CHAT, human(BOB))

        assert rta.current_round_speaker(CHAT) == ALICE


class TestTheFourRefusals:
    def test_two_humans_give_no_speaker(self):
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.note_pending_trigger(CHAT, human(BOB))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_a_non_human_trigger_gives_no_speaker(self):
        """有定时器时说话的人可能根本没开口，改的就不是「他自己的」设置"""
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.note_pending_trigger(CHAT, timer())
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_a_human_trigger_without_a_sender_gives_no_speaker(self):
        rta.note_pending_trigger(CHAT, RoundTrigger(kind="human", sender_id=""))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_an_overflowed_batch_gives_no_speaker(self):
        """丢过条目就不能再说「只剩一个人」：丢掉的那个可能就是另一个发言人"""
        for index in range(rta.MAX_TRIGGER_ACCOUNTS + 1):
            rta.note_pending_trigger(CHAT, human(f"user{index:04d}"))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_an_expired_record_gives_no_speaker(self):
        """回合卡住、结束没跑到时，旧记录不能一直有效"""
        base = rta._clock()  # noqa: SLF001
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT, now=base + rta.SPEAKER_TTL_SECONDS + 1) is None

    def test_the_record_is_still_valid_just_before_it_expires(self):
        base = rta._clock()  # noqa: SLF001
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT, now=base + rta.SPEAKER_TTL_SECONDS - 1) == ALICE

    def test_the_injected_clock_drives_the_expiry(self):
        """时钟可注入：用例不必真的等 10 分钟，生产里用的仍是 `time.monotonic`"""
        now = 1000.0
        rta._clock = lambda: now  # noqa: SLF001
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)
        assert rta.current_round_speaker(CHAT) == ALICE

        now += rta.SPEAKER_TTL_SECONDS + 1
        assert rta.current_round_speaker(CHAT) is None


class TestTheRecordLifecycle:
    def test_a_round_without_any_pending_trigger_has_no_speaker(self):
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None
        assert rta.current_round_trigger_accounts(CHAT) == (rta.UNKNOWN_TRIGGER_ACCOUNT,)

    def test_beginning_a_new_round_does_not_reuse_the_previous_speaker(self):
        """上一轮的人不能成为这一轮的发言人：没有触发就是没有"""
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)
        rta.clear_round_triggers(CHAT)
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_a_new_round_without_triggers_replaces_an_unclosed_record(self):
        """上一轮没收尾（`clear_round_triggers` 没跑到）时，新一轮也不能沿用上一轮的人"""
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)
        assert rta.current_round_speaker(CHAT) == ALICE

        rta.begin_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_clearing_the_round_removes_the_speaker(self):
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)
        rta.clear_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None

    def test_channels_do_not_share_their_speaker(self):
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.note_pending_trigger(OTHER, human(BOB))
        rta.begin_round_triggers(CHAT)
        rta.begin_round_triggers(OTHER)

        assert rta.current_round_speaker(CHAT) == ALICE
        assert rta.current_round_speaker(OTHER) == BOB

    def test_an_empty_chat_key_is_never_a_speaker(self):
        assert rta.current_round_speaker("") is None
        rta.note_pending_trigger("", human(ALICE))
        rta.begin_round_triggers("")
        assert rta.current_round_speaker("") is None


class TestTheFirstRecordIsUnchanged:
    def test_the_log_list_still_carries_accounts_and_kinds(self):
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.note_pending_trigger(CHAT, timer())
        rta.begin_round_triggers(CHAT)

        assert rta.current_round_trigger_accounts(CHAT) == (ALICE, "timer")

    def test_the_log_list_keeps_its_cap(self):
        for index in range(rta.MAX_TRIGGER_ACCOUNTS + 5):
            rta.note_pending_trigger(CHAT, human(f"user{index:04d}"))
        rta.begin_round_triggers(CHAT)

        assert len(rta.current_round_trigger_accounts(CHAT)) == rta.MAX_TRIGGER_ACCOUNTS

    def test_the_two_records_do_not_import_each_other(self):
        """两份记录同批攒、同批清：结构化记录清掉了，日志列表也一定清掉了"""
        rta.note_pending_trigger(CHAT, human(ALICE))
        rta.begin_round_triggers(CHAT)
        rta.clear_round_triggers(CHAT)

        assert rta.current_round_speaker(CHAT) is None
        assert rta.current_round_trigger_accounts(CHAT) == (rta.UNKNOWN_TRIGGER_ACCOUNT,)


class TestTheModuleDoesNotBorrowTheOpsBook:
    def test_the_code_never_reaches_for_the_ops_trigger_book(self):
        """运维那一侧的触发记录簿默认关着、`peek` 也不按存活时间过滤，不能借它判发言人

        文档字符串里会提到它（那是「为什么不用」的理由），所以判的是**代码里没有引用**：
        没有 import、没有名字、没有属性访问。
        """
        tree = ast.parse(Path(rta.__file__).read_text(encoding="utf-8"))
        from_ops: set = set()
        referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("nekro_agent.services.ops_agent"):
                    from_ops.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)

        assert "round_trigger_book" not in referenced
        assert from_ops == {"TRIGGER_KIND_HUMAN", "RoundTrigger"}, "只借触发种类的常量与那个小数据类"
