"""内置插件「消息撤回」与历史时间解析

## 这组用例盯什么

- 历史里渲染出来的时间能被解析回同一个 `send_timestamp`，换时区也一致；
- 模型用「发送者 id + 时间」或 `message_id` 指认消息时，只在当前频道里找、只撤该撤的那几条；
- 协议端各种失败原样交给模型，失败时补一句双方身份；结果里的昵称与协议端原话过 `echo_inline`；
- 每个频道每小时的次数上限、以及日志里不出现目标正文；
- 插件只在群聊里出现、默认元数据正确；
- `run_agent` 在回合开始记触发者、结束时清掉。

测试数据全部是运行时拼出来的虚构短文本；「敏感内容」只用于验证「不会进日志」。
"""

import ast
import asyncio
import datetime
import inspect
import re
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
from nonebot.adapters.onebot.v11.exception import ActionFailed
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import recall as recall_module
from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.agent import round_trigger_accounts
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.ops_agent.triggers import RoundTrigger
from nekro_agent.tools import history_time
from plugins.builtin import message_recall

CHAT_KEY = "onebot_v11-probe-group_10001"
OTHER_CHAT_KEY = "onebot_v11-probe-group_20002"
CHANNEL_ID = "group_10001"
BOT_ACCOUNT = "90000001"
TARGET_ACCOUNT = "12345"
TRIGGER_ACCOUNT = "55501"

# 正文渲染只需要这几个字段
RENDER_CONFIG = SimpleNamespace(
    AI_CONTEXT_LENGTH_PER_MESSAGE=100_000,
    AI_SHOW_REMOTE_URL=False,
    AI_INCLUDE_TOME_INDICATOR=False,
)


def ctx_of(
    chat_key: str = CHAT_KEY,
    *,
    channel_type: str = "group",
    channel_id: str = CHANNEL_ID,
) -> AgentCtx:
    return AgentCtx(
        from_chat_key=chat_key,
        container_key="probe",
        channel_type=channel_type,
        channel_id=channel_id,
        adapter_key="onebot_v11",
    )


def past_timestamp(seconds: int = 120) -> int:
    return int(time.time()) - seconds


async def add_message(
    *,
    chat_key: str = CHAT_KEY,
    message_id: str = "1001",
    platform_userid: str = TARGET_ACCOUNT,
    sender_id: str = "",
    text: str = "虚构的短文本",
    timestamp: int = 0,
    is_recalled: bool = False,
    sender_nickname: str = "小明",
    sender_name: str = "小明",
    chat_type: str = "group",
) -> DBChatMessage:
    return await DBChatMessage.create(
        message_id=message_id,
        sender_id=sender_id or platform_userid,
        sender_name=sender_name,
        sender_nickname=sender_nickname,
        platform_userid=platform_userid,
        is_tome=0,
        is_recalled=is_recalled,
        adapter_key="onebot_v11",
        instance_key="probe",
        chat_key=chat_key,
        chat_type=chat_type,
        content_text=text,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=timestamp or past_timestamp(),
    )


def time_text(timestamp: int) -> str:
    return history_time.format_history_time(timestamp)


class FakeBot:
    """记录 `call_api`、可按动作配置返回值或异常的假连接"""

    self_id = BOT_ACCOUNT

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.handlers: Dict[str, Any] = {}
        # `get_group_member_info` 的默认回包：撤回失败的用例多数只需要一个合法身份
        self.handlers["get_group_member_info"] = lambda **_kwargs: {"role": "member"}

    async def call_api(self, action: str, **kwargs: Any) -> Any:
        self.calls.append((action, kwargs))
        handler = self.handlers.get(action)
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            result = handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        return handler

    def actions(self) -> List[str]:
        return [action for action, _kwargs in self.calls]

    def delete_ids(self) -> List[int]:
        return [kwargs["message_id"] for action, kwargs in self.calls if action == "delete_msg"]


class _LogRecorder:
    """替换 `plugin.logger`，把 INFO 行收下来"""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def info(self, message: Any, *_args: Any, **_kwargs: Any) -> None:
        self.lines.append(str(message))

    def __getattr__(self, _level: str):
        return lambda *_args, **_kwargs: None


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_plugin_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=10))
    message_recall._reset_for_tests()
    recall_module.reset_role_cache()
    round_trigger_accounts.reset_round_triggers()
    yield
    message_recall._reset_for_tests()
    recall_module.reset_role_cache()
    round_trigger_accounts.reset_round_triggers()


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> FakeBot:
    fake = FakeBot()

    async def fake_get_bot(_chat_key: str) -> FakeBot:
        return fake

    monkeypatch.setattr(recall_module, "get_bot_by_chat_key", fake_get_bot)
    return fake


# ---------------------------------------------------------------------------
# A. 历史时间
# ---------------------------------------------------------------------------


class TestHistoryTime:
    def test_local_timezone_round_trip_is_byte_identical(self):
        timestamp = past_timestamp(3600)
        expected = datetime.datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M:%S")
        assert history_time.format_history_time(timestamp) == expected
        assert history_time.parse_history_time(expected, now=timestamp + 5) == timestamp

    async def test_render_and_parse_share_the_configured_timezone(self, db, monkeypatch: pytest.MonkeyPatch):
        local_offset = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
        fixed = datetime.timezone(local_offset + datetime.timedelta(hours=3))
        monkeypatch.setattr(history_time, "history_timezone", lambda: fixed)

        timestamp = past_timestamp(7200)
        row = await add_message(timestamp=timestamp)
        prompt = row.parse_chat_history_prompt("tok", RENDER_CONFIG)  # type: ignore[arg-type]
        matched = re.search(r"\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) id:", prompt)
        assert matched is not None, prompt
        rendered = matched.group(1)

        assert history_time.parse_history_time(rendered, now=timestamp + 5) == timestamp

        # 同一个字符串按本机本地时区解析应当落在另一个时刻：证明这条用例真的跨了两个时区
        with monkeypatch.context() as patch:
            patch.setattr(history_time, "history_timezone", lambda: None)
            local_value = history_time.parse_history_time(rendered, now=timestamp + 5)
        assert local_value is not None
        assert local_value != timestamp

    def test_cross_year_looks_back_one_year(self):
        now = datetime.datetime(2026, 1, 1, 0, 0, 30).timestamp()
        parsed = history_time.parse_history_time("12-31 23:59:50", now=now)
        assert parsed == int(datetime.datetime(2025, 12, 31, 23, 59, 50).timestamp())

    def test_future_times_fall_back_to_the_previous_year(self):
        now = datetime.datetime(2026, 6, 1, 12, 0, 0).timestamp()
        parsed = history_time.parse_history_time("12-31 10:00:00", now=now)
        assert parsed == int(datetime.datetime(2025, 12, 31, 10, 0, 0).timestamp())

    def test_feb_29_uses_the_previous_leap_year_or_gives_up(self):
        # 2027 与 2026 都不是闰年：两年都 strptime 失败，只能返回 None
        assert history_time.parse_history_time("02-29 10:00:00", now=datetime.datetime(2027, 6, 1).timestamp()) is None

        # 2025 不是闰年、上一年 2024 是：按规则能解析到 2024-02-29
        parsed = history_time.parse_history_time("02-29 10:00:00", now=datetime.datetime(2025, 6, 1).timestamp())
        assert parsed == int(datetime.datetime(2024, 2, 29, 10, 0, 0).timestamp())

    @pytest.mark.parametrize(
        "text",
        [
            "11-9 10:00",
            "2026-11-09 10:00:00",
            "",
            "11-09\n10:00:00",
            "不是时间",
        ],
    )
    def test_malformed_times_are_rejected(self, text: str):
        assert history_time.parse_history_time(text) is None
        assert history_time.history_time_candidates(text) == []

    def test_a_past_date_has_two_candidate_years(self):
        """今年这个日子已经过去：今年、去年两个候选都在过去，光凭这串字分不出来"""
        now = datetime.datetime(2026, 11, 10, 12, 0, 0).timestamp()
        candidates = history_time.history_time_candidates("10-01 12:00:00", now=now)
        assert candidates == [
            int(datetime.datetime(2026, 10, 1, 12, 0, 0).timestamp()),
            int(datetime.datetime(2025, 10, 1, 12, 0, 0).timestamp()),
        ]
        assert history_time.parse_history_time("10-01 12:00:00", now=now) == candidates[0]

    def test_a_future_date_this_year_leaves_only_last_year(self):
        now = datetime.datetime(2026, 6, 1, 12, 0, 0).timestamp()
        assert history_time.history_time_candidates("12-31 10:00:00", now=now) == [
            int(datetime.datetime(2025, 12, 31, 10, 0, 0).timestamp()),
        ]


# ---------------------------------------------------------------------------
# B. 指认与分拣
# ---------------------------------------------------------------------------


class TestTargeting:
    async def test_user_id_and_time_recall_one_message(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        row = await add_message(message_id="1515", timestamp=timestamp, text="虚构口令：abc")

        result = await message_recall.recall_message(
            ctx_of(),
            user_id=TARGET_ACCOUNT,
            send_time=time_text(timestamp),
        )

        assert result.startswith("[撤回成功]")
        assert "1 条消息" in result
        assert bot.calls == [("delete_msg", {"message_id": 1515})]
        fresh = await DBChatMessage.get(id=row.id)
        assert fresh.is_recalled is True
        assert fresh.content_text == "虚构口令：abc"

    async def test_same_person_same_second_recalls_all(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="2001", timestamp=timestamp)
        await add_message(message_id="2002", timestamp=timestamp)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤回成功]")
        assert "2 条消息" in result
        assert bot.delete_ids() == [2001, 2002]

    async def test_negative_message_ids_are_normal_ids(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="-1515", timestamp=timestamp)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤回成功]")
        assert bot.delete_ids() == [-1515]

    async def test_message_without_id_cannot_be_recalled(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="", timestamp=timestamp)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤不了]")
        assert bot.calls == []

    async def test_only_numbered_rows_in_the_same_second_are_recalled(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="", timestamp=timestamp)
        await add_message(message_id="3001", timestamp=timestamp)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤回成功]")
        assert "1 条消息" in result
        assert bot.delete_ids() == [3001]

    async def test_already_recalled_is_not_sent_to_the_protocol(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="4001", timestamp=timestamp, is_recalled=True)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[已撤回过]")
        assert bot.calls == []

    async def test_other_channels_are_invisible(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        other_row = await add_message(chat_key=OTHER_CHAT_KEY, message_id="7777", timestamp=timestamp)
        await add_message(message_id="8888", timestamp=timestamp + 1)

        by_time = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))
        by_id = await message_recall.recall_message(ctx_of(), message_id="7777")

        assert by_time.startswith("[没找到]")
        assert by_id.startswith("[没找到]")
        assert bot.calls == []
        assert (await DBChatMessage.get(id=other_row.id)).is_recalled is False

    async def test_system_messages_are_not_targets(self, db, bot: FakeBot):
        await add_message(message_id="5001", platform_userid="0", text="宿主提示")

        result = await message_recall.recall_message(ctx_of(), message_id="5001")

        assert result.startswith("[没找到]")
        assert bot.calls == []

    async def test_message_id_targeting(self, db, bot: FakeBot):
        await add_message(message_id="6060")

        result = await message_recall.recall_message(ctx_of(), message_id="6060")

        assert result.startswith("[撤回成功]")
        assert bot.delete_ids() == [6060]

    @pytest.mark.parametrize("bad_id", ["007", "abc"])
    async def test_malformed_message_id_is_usage_without_touching_the_database(
        self,
        db,
        bot: FakeBot,
        monkeypatch: pytest.MonkeyPatch,
        bad_id: str,
    ):
        class _BoomMessage:
            @staticmethod
            def filter(*_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("bad_args 不应查库")

        monkeypatch.setattr(message_recall, "DBChatMessage", _BoomMessage)

        result = await message_recall.recall_message(ctx_of(), message_id=bad_id)

        assert result.startswith("[用法]")
        assert bot.calls == []
        assert not message_recall._recall_windows.get(CHAT_KEY)

    @pytest.mark.parametrize("bad_user", ["１２３４５", "12³45", "0", "", "12a45"])
    async def test_user_id_must_be_ascii_digits(self, db, bot: FakeBot, bad_user: str):
        """全角数字、上标数字 `isdigit` 也认，只能按 ASCII 数字收"""
        timestamp = past_timestamp()
        result = await message_recall.recall_message(ctx_of(), user_id=bad_user, send_time=time_text(timestamp))

        assert result.startswith("[用法]")
        assert bot.calls == []
        assert not message_recall._recall_windows.get(CHAT_KEY)

    async def test_same_second_in_two_years_is_refused(self, db, bot: FakeBot):
        """历史穿越能翻出一年前的消息；同一个人两年同一秒都说过话时，按时间撤哪条都可能撤错"""
        this_year = past_timestamp(3600)
        rendered = time_text(this_year)
        if rendered.startswith("02-29"):
            pytest.skip("去年没有 2 月 29 日")
        last_year_dt = datetime.datetime.fromtimestamp(this_year).replace(year=datetime.datetime.fromtimestamp(this_year).year - 1)
        last_year = int(last_year_dt.timestamp())
        assert time_text(last_year) == rendered
        await add_message(message_id="8801", timestamp=this_year)
        await add_message(message_id="8802", timestamp=last_year)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=rendered)

        assert result.startswith("[说不清是哪条]")
        assert bot.delete_ids() == []
        assert not message_recall._recall_windows.get(CHAT_KEY)

        # 改用编号就能撤
        by_id = await message_recall.recall_message(ctx_of(), message_id="8802")
        assert by_id.startswith("[撤回成功]")
        assert bot.delete_ids() == [8802]

    async def test_a_message_from_last_year_alone_is_found(self, db, bot: FakeBot):
        this_year = past_timestamp(3600)
        if time_text(this_year).startswith("02-29"):
            pytest.skip("去年没有 2 月 29 日")
        last_year_dt = datetime.datetime.fromtimestamp(this_year).replace(year=datetime.datetime.fromtimestamp(this_year).year - 1)
        last_year = int(last_year_dt.timestamp())
        await add_message(message_id="8803", timestamp=last_year)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(last_year))

        assert result.startswith("[撤回成功]")
        assert bot.delete_ids() == [8803]

    async def test_bot_own_message_is_found_by_bot_account(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="7001", platform_userid=BOT_ACCOUNT, sender_id="-1", timestamp=timestamp)

        result = await message_recall.recall_message(ctx_of(), user_id=BOT_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤回成功]")
        assert bot.delete_ids() == [7001]


# ---------------------------------------------------------------------------
# C. 协议端结果与身份
# ---------------------------------------------------------------------------


class TestProtocolResults:
    async def test_action_failed_is_reported_and_identity_is_added(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="8001", timestamp=timestamp)
        wording = '第一行\n"带引号" 的协议端原话'
        bot.handlers["delete_msg"] = ActionFailed(retcode=100, wording=wording)

        def role(**kwargs: Any) -> Dict[str, str]:
            return {"role": "member" if str(kwargs["user_id"]) == BOT_ACCOUNT else "admin"}

        bot.handlers["get_group_member_info"] = role

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤回失败]")
        assert "\n" not in result
        assert "第一行" in result and "带引号" in result
        assert "你在这个群是普通成员，对方是管理员。" in result

    async def test_timeout_has_its_own_message(self, db, bot: FakeBot, monkeypatch: pytest.MonkeyPatch):
        timestamp = past_timestamp()
        await add_message(message_id="8101", timestamp=timestamp)

        async def slow_delete(**_kwargs: Any) -> Dict[str, Any]:
            await asyncio.sleep(0.05)
            return {}

        bot.handlers["delete_msg"] = slow_delete

        async def fast_timeout(chat_key: str, message_id: str):
            return await recall_module.recall_group_message(chat_key, message_id, timeout=0.01)

        monkeypatch.setattr(message_recall, "recall_group_message", fast_timeout)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result == "[撤回失败] 协议端 10 秒内没有回应，可以稍后再试一次。"

    async def test_offline_bot_is_reported(self, db, monkeypatch: pytest.MonkeyPatch):
        timestamp = past_timestamp()
        await add_message(message_id="8201", timestamp=timestamp)

        async def offline(_chat_key: str) -> Any:
            raise BotInstanceOfflineError("probe")

        monkeypatch.setattr(recall_module, "get_bot_by_chat_key", offline)

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert "没有连上协议端" in result

    async def test_one_success_and_one_failure_is_partial(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        ok_row = await add_message(message_id="8301", timestamp=timestamp)
        fail_row = await add_message(message_id="8302", timestamp=timestamp)

        def delete(**kwargs: Any) -> Dict[str, Any]:
            if kwargs["message_id"] == 8302:
                raise ActionFailed(retcode=1, wording="这条撤不了")
            return {}

        bot.handlers["delete_msg"] = delete

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[部分撤回]")
        assert "撤回了 1 条" in result
        assert "1 条没撤成" in result
        assert (await DBChatMessage.get(id=ok_row.id)).is_recalled is True
        assert (await DBChatMessage.get(id=fail_row.id)).is_recalled is False

    async def test_different_failure_reasons_are_all_reported(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="8311", timestamp=timestamp)
        await add_message(message_id="8312", timestamp=timestamp)
        await add_message(message_id="8313", timestamp=timestamp)

        def delete(**kwargs: Any) -> Dict[str, Any]:
            if kwargs["message_id"] == 8311:
                return {}
            raise ActionFailed(retcode=1, wording="原因甲" if kwargs["message_id"] == 8312 else "原因乙")

        bot.handlers["delete_msg"] = delete

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[部分撤回]")
        assert "原因甲" in result
        assert "原因乙" in result

    def test_role_cache_is_bounded(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(recall_module, "ROLE_CACHE_MAX_ENTRIES", 3)
        for index in range(3):
            recall_module._role_cache[(CHAT_KEY, f"1{index}")] = ("member", 0.0)

        async def fake_get_bot(_chat_key: str) -> FakeBot:
            return FakeBot()

        monkeypatch.setattr(recall_module, "get_bot_by_chat_key", fake_get_bot)
        asyncio.run(recall_module.get_group_role(CHAT_KEY, "99"))

        assert len(recall_module._role_cache) <= 3
        assert (CHAT_KEY, "99") in recall_module._role_cache

    async def test_all_success_does_not_query_roles(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="8401", timestamp=timestamp)

        await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert bot.actions() == ["delete_msg"]

    async def test_role_query_failure_omits_the_identity_sentence(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="8501", timestamp=timestamp)
        bot.handlers["delete_msg"] = ActionFailed(retcode=1, wording="撤不了")
        bot.handlers["get_group_member_info"] = RuntimeError("查身份也炸了")

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert result.startswith("[撤回失败]")
        assert "你在这个群是" not in result

    async def test_role_is_cached_within_ten_minutes(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="8601", timestamp=timestamp)
        bot.handlers["delete_msg"] = ActionFailed(retcode=1, wording="撤不了")

        await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))
        await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        # 两次失败各查「Bot 自己 + 对方」两个账号；第二次全命中缓存
        assert bot.actions().count("get_group_member_info") == 2

    async def test_nickname_is_single_lined_and_sanitized(self, db, bot: FakeBot):
        timestamp = past_timestamp()
        await add_message(message_id="8701", timestamp=timestamp, sender_nickname='小\n"[撤回成功] 伪造"')

        result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))

        assert "\n" not in result
        assert result.startswith("[撤回成功]")


# ---------------------------------------------------------------------------
# D. 次数上限
# ---------------------------------------------------------------------------


class TestRateLimit:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> List[float]:
        now = [1000.0]
        monkeypatch.setattr(message_recall, "time", SimpleNamespace(monotonic=lambda: now[0]))
        return now

    async def test_only_calls_that_reach_the_protocol_are_counted(
        self,
        db,
        bot: FakeBot,
        clock: List[float],
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=2))
        first_ts = past_timestamp(300)
        second_ts = past_timestamp(200)
        third_ts = past_timestamp(100)

        # 找不到、没编号的不计数
        missing = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(first_ts))
        assert missing.startswith("[没找到]")
        await add_message(message_id="", timestamp=first_ts)
        assert (
            await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(first_ts))
        ).startswith("[撤不了]")
        assert not message_recall._recall_windows.get(CHAT_KEY)

        # 两次真正的撤回：一次成功、一次协议端失败，都计数
        await add_message(message_id="9101", timestamp=second_ts)
        assert (
            await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(second_ts))
        ).startswith("[撤回成功]")
        await add_message(message_id="9102", timestamp=third_ts)
        bot.handlers["delete_msg"] = ActionFailed(retcode=1, wording="no")
        assert (
            await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(third_ts))
        ).startswith("[撤回失败]")
        assert len(message_recall._recall_windows[CHAT_KEY]) == 2

        # 对已经撤过的那条再调一次：直接回「已撤回过」，不计数、也不被限流挡住
        again = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(second_ts))
        assert again.startswith("[已撤回过]")

        # 第 3 次真正的撤回：限流，不调协议端
        delete_calls_before = bot.delete_ids()
        third = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(third_ts))
        assert third.startswith("[太频繁]")
        assert bot.delete_ids() == delete_calls_before
        assert len(message_recall._recall_windows[CHAT_KEY]) == 2

        # 3600 秒后窗口滑空，恢复
        bot.handlers.pop("delete_msg")
        clock[0] += 3601
        recovered = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(third_ts))
        assert recovered.startswith("[撤回成功]")

    async def test_zero_means_unlimited(self, db, bot: FakeBot, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=0))
        for index in range(5):
            timestamp = past_timestamp(100 + index)
            await add_message(message_id=f"93{index:02d}", timestamp=timestamp)
            result = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp))
            assert result.startswith("[撤回成功]")
        assert not message_recall._recall_windows.get(CHAT_KEY)

    @pytest.mark.parametrize("bad_value", [-1, -100, "abc"])
    async def test_negative_or_broken_limit_falls_back_to_the_default(
        self,
        db,
        bot: FakeBot,
        monkeypatch: pytest.MonkeyPatch,
        bad_value: Any,
    ):
        """后台写插件配置不跑 `ge=0`，负数不能被当成「不限」"""
        monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=bad_value))
        results = []
        for index in range(message_recall.DEFAULT_MAX_PER_HOUR + 1):
            timestamp = past_timestamp(100 + index)
            await add_message(message_id=f"94{index:02d}", timestamp=timestamp)
            results.append(
                await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp)),
            )
        assert all(result.startswith("[撤回成功]") for result in results[:-1])
        assert results[-1].startswith("[太频繁]")

    async def test_bad_args_are_not_counted(self, db, bot: FakeBot, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=1))
        first_ts = past_timestamp(200)
        second_ts = past_timestamp(100)

        bad = await message_recall.recall_message(ctx_of(), user_id="", send_time="")
        assert bad.startswith("[用法]")
        assert not message_recall._recall_windows.get(CHAT_KEY)

        await add_message(message_id="9201", timestamp=first_ts)
        await add_message(message_id="9202", timestamp=second_ts)
        first = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(first_ts))
        assert first.startswith("[撤回成功]")
        second = await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(second_ts))
        assert second.startswith("[太频繁]")

    async def test_channels_have_separate_windows(self, db, bot: FakeBot, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=1))
        first_ts = past_timestamp(200)
        second_ts = past_timestamp(100)
        other_ctx = ctx_of(OTHER_CHAT_KEY, channel_id="group_20002")
        for chat_key, prefix in ((CHAT_KEY, "95"), (OTHER_CHAT_KEY, "96")):
            await add_message(chat_key=chat_key, message_id=f"{prefix}01", timestamp=first_ts)
            await add_message(chat_key=chat_key, message_id=f"{prefix}02", timestamp=second_ts)

        for ctx in (ctx_of(), other_ctx):
            assert (
                await message_recall.recall_message(ctx, user_id=TARGET_ACCOUNT, send_time=time_text(first_ts))
            ).startswith("[撤回成功]")
        for ctx in (ctx_of(), other_ctx):
            assert (
                await message_recall.recall_message(ctx, user_id=TARGET_ACCOUNT, send_time=time_text(second_ts))
            ).startswith("[太频繁]")

    def test_the_window_table_drops_fully_expired_channels(self):
        message_recall._recall_windows.clear()
        for index in range(message_recall.MAX_TRACKED_CHANNELS + 5):
            message_recall._recall_windows[f"stale-{index}"] = deque([0.0])
        message_recall._recall_windows["fresh"] = deque([10_000.0])

        message_recall._window_of("new", 10_100.0)

        assert "fresh" in message_recall._recall_windows
        assert "new" in message_recall._recall_windows
        assert not [key for key in message_recall._recall_windows if key.startswith("stale-")]


# ---------------------------------------------------------------------------
# E. 日志
# ---------------------------------------------------------------------------


def _six_char_windows(text: str) -> List[str]:
    squashed = "".join(text.split())
    return [squashed[start : start + 6] for start in range(len(squashed) - 5)]


class TestLogging:
    @pytest.fixture
    def logs(self, monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
        recorder = _LogRecorder()
        monkeypatch.setattr(message_recall.plugin, "logger", recorder)
        return recorder

    async def test_every_call_logs_one_line_without_the_message_body(self, db, bot: FakeBot, logs: _LogRecorder):
        secret = "sk-" + "A" * 40
        body = f"临时凭据 {secret} 请尽快处理"
        ok_timestamp = past_timestamp()
        fail_timestamp = past_timestamp(60)
        await add_message(message_id="9301", timestamp=ok_timestamp, text=body)
        round_trigger_accounts.note_pending_trigger(CHAT_KEY, RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT))
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)

        # 一次成功
        await message_recall.recall_message(
            ctx_of(),
            user_id=TARGET_ACCOUNT,
            send_time=time_text(ok_timestamp),
            reason="误发",
        )

        # 一次失败
        await add_message(message_id="9302", timestamp=fail_timestamp, text=body)
        bot.handlers["delete_msg"] = ActionFailed(retcode=1, wording="撤不了")
        await message_recall.recall_message(
            ctx_of(),
            user_id=TARGET_ACCOUNT,
            send_time=time_text(fail_timestamp),
            reason="误发",
        )

        assert len(logs.lines) == 2
        assert "结果=ok" in logs.lines[0]
        assert "结果=failed" in logs.lines[1]
        for line in logs.lines:
            assert CHAT_KEY in line
            assert TRIGGER_ACCOUNT in line
            assert "9301" in line or "9302" in line
        combined = "\n".join(logs.lines)
        for window in _six_char_windows(body):
            assert window not in combined, window

    async def test_reason_that_copies_the_body_is_replaced(self, db, bot: FakeBot, logs: _LogRecorder):
        filler = "Q" * 30
        body = f"虚构内容 {filler} 结束"
        timestamp = past_timestamp()
        await add_message(message_id="9401", timestamp=timestamp, text=body)

        await message_recall.recall_message(
            ctx_of(),
            user_id=TARGET_ACCOUNT,
            send_time=time_text(timestamp),
            reason=f"请撤回这条：{filler}",
        )

        assert "（理由里带了原文，已丢弃）" in logs.lines[0]
        assert filler not in logs.lines[0]

    @pytest.mark.parametrize("path", ["bad_args", "not_found", "rate_limited"])
    async def test_reason_is_never_logged_when_there_is_nothing_to_compare_or_it_copies_the_body(
        self,
        db,
        bot: FakeBot,
        logs: _LogRecorder,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
    ):
        """被拒的调用里理由照样可能抄了正文：没有目标可比对时一个字都不记，限流时按目标正文清洗"""
        filler = "Z" * 30
        reason = f"请撤回这条：{filler}"
        timestamp = past_timestamp()
        if path == "bad_args":
            await message_recall.recall_message(ctx_of(), user_id="not-a-number", send_time="?", reason=reason)
        elif path == "not_found":
            await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp), reason=reason)
        else:
            monkeypatch.setattr(message_recall, "config", SimpleNamespace(MAX_PER_HOUR=1))
            await add_message(message_id="9701", timestamp=timestamp, text=f"虚构 {filler}")
            await add_message(message_id="9702", timestamp=timestamp + 1, text=f"虚构 {filler}")
            await message_recall.recall_message(ctx_of(), user_id=TARGET_ACCOUNT, send_time=time_text(timestamp), reason="误发")
            logs.lines.clear()
            limited = await message_recall.recall_message(
                ctx_of(),
                user_id=TARGET_ACCOUNT,
                send_time=time_text(timestamp + 1),
                reason=reason,
            )
            assert limited.startswith("[太频繁]")

        assert len(logs.lines) == 1
        assert filler not in logs.lines[0]
        assert "请撤回这条" not in logs.lines[0]

    async def test_normal_reason_is_single_lined_and_truncated(self, db, bot: FakeBot, logs: _LogRecorder):
        timestamp = past_timestamp()
        await add_message(message_id="9501", timestamp=timestamp, text="与理由无关的虚构内容")
        long_reason = "误发\n" + "说明" * 60

        await message_recall.recall_message(
            ctx_of(),
            user_id=TARGET_ACCOUNT,
            send_time=time_text(timestamp),
            reason=long_reason,
        )

        expected = " ".join(long_reason.split())[:80]
        assert f"理由={expected}" in logs.lines[0]

    async def test_result_does_not_echo_model_arguments(self, db, bot: FakeBot):
        timestamp = past_timestamp()

        # 没找到时返回的是固定文案：user_id / send_time / reason 一个都不该出现
        missing_user = "67890"
        missing_send_time = time_text(timestamp)
        missing_reason = "REASON-SHOULD-NOT-APPEAR"
        missing = await message_recall.recall_message(
            ctx_of(),
            user_id=missing_user,
            send_time=missing_send_time,
            reason=missing_reason,
        )
        assert missing.startswith("[没找到]")
        for value in (missing_user, missing_send_time, missing_reason):
            assert value not in missing

        # 成功时时间由宿主按库里的时间戳算出；带上首尾空白的原始参数不会被原样拼回去
        await add_message(message_id="9601", timestamp=timestamp)
        raw_send_time = f"\t{time_text(timestamp)}\t"
        result = await message_recall.recall_message(
            ctx_of(),
            user_id=TARGET_ACCOUNT,
            send_time=raw_send_time,
            reason="REASON-SHOULD-NOT-APPEAR",
        )

        assert result.startswith("[撤回成功]")
        assert TARGET_ACCOUNT not in result
        assert "REASON-SHOULD-NOT-APPEAR" not in result
        assert raw_send_time not in result
        assert time_text(timestamp) in result


# ---------------------------------------------------------------------------
# F. 方法出现与否、插件元数据
# ---------------------------------------------------------------------------


class TestAvailability:
    async def test_group_only(self):
        assert await message_recall.collect_available_methods(ctx_of()) == [message_recall.recall_message]
        private = ctx_of(channel_type="private", channel_id="private_10001")
        assert await message_recall.collect_available_methods(private) == []

    async def test_empty_channel_type_falls_back_to_the_channel_id(self):
        fallback = ctx_of(channel_type="", channel_id="group_1")
        assert await message_recall.collect_available_methods(fallback) == [message_recall.recall_message]

    async def test_enum_channel_type_is_read_through_value(self):
        enum_ctx = SimpleNamespace(channel_type=ChatType.GROUP, channel_id=CHANNEL_ID)
        assert await message_recall.collect_available_methods(enum_ctx) == [message_recall.recall_message]

    def test_plugin_metadata(self):
        plugin = message_recall.plugin
        assert plugin.support_adapter == ["onebot_v11"]
        assert plugin.allow_sleep is True
        assert plugin.sleep_brief
        assert plugin.module_name == "message_recall"


# ---------------------------------------------------------------------------
# 默认启用：配置文件整份落盘，新内置插件只能在启动时加进 PLUGIN_ENABLED 一次
# ---------------------------------------------------------------------------


class TestDefaultOn:
    @staticmethod
    def cfg(enabled: List[str], applied: List[str]) -> SimpleNamespace:
        return SimpleNamespace(PLUGIN_ENABLED=list(enabled), PLUGIN_DEFAULT_ON_APPLIED=list(applied))

    def test_the_plugin_key_is_in_the_default_on_list(self):
        from nekro_agent.services.plugin.default_on import DEFAULT_ON_BUILTIN_PLUGINS

        assert message_recall.plugin.key in DEFAULT_ON_BUILTIN_PLUGINS

    def test_existing_deployment_gets_it_once(self):
        from nekro_agent.services.plugin.default_on import apply_default_on_plugins

        cfg = self.cfg(["KroMiose.basic"], [])
        newly = apply_default_on_plugins(cfg, ("Akiyo_dayo.message_recall",))

        assert newly == ["Akiyo_dayo.message_recall"]
        assert cfg.PLUGIN_ENABLED == ["KroMiose.basic", "Akiyo_dayo.message_recall"]
        assert cfg.PLUGIN_DEFAULT_ON_APPLIED == ["Akiyo_dayo.message_recall"]

        # 再来一次（同一次启动里 init_plugins 又调了一遍）：已经应用过，什么都不改
        assert apply_default_on_plugins(cfg, ("Akiyo_dayo.message_recall",)) == []
        assert cfg.PLUGIN_ENABLED == ["KroMiose.basic", "Akiyo_dayo.message_recall"]
        assert cfg.PLUGIN_DEFAULT_ON_APPLIED == ["Akiyo_dayo.message_recall"]

    def test_platform_disabling_it_afterwards_sticks(self):
        from nekro_agent.services.plugin.default_on import apply_default_on_plugins

        cfg = self.cfg(["KroMiose.basic"], ["Akiyo_dayo.message_recall"])
        assert apply_default_on_plugins(cfg, ("Akiyo_dayo.message_recall",)) == []
        assert "Akiyo_dayo.message_recall" not in cfg.PLUGIN_ENABLED

    def test_already_enabled_is_recorded_without_duplicating(self):
        from nekro_agent.services.plugin.default_on import apply_default_on_plugins

        cfg = self.cfg(["Akiyo_dayo.message_recall"], [])
        newly = apply_default_on_plugins(cfg, ("Akiyo_dayo.message_recall",))

        assert newly == []
        assert cfg.PLUGIN_ENABLED == ["Akiyo_dayo.message_recall"]
        assert cfg.PLUGIN_DEFAULT_ON_APPLIED == ["Akiyo_dayo.message_recall"]

    def test_startup_hook_only_touches_memory(self, monkeypatch: pytest.MonkeyPatch):
        """落盘会掩码模型组 Key；`bootstrap_model_groups()` 为保住 YAML 里唯一的明文而不落盘时，
        这里若自己再落一次就把那层保护绕过去了。所以这里绝不调任何落盘入口"""
        import importlib

        from nekro_agent.services import config_service
        from nekro_agent.services.plugin import default_on

        # `nekro_agent.core` 把配置对象也叫 `config` 导出了，按模块名取才拿得到模块本身
        config_module = importlib.import_module("nekro_agent.core.config")

        fake = self.cfg(["KroMiose.basic"], [])
        fake.dump_config = lambda *_args, **_kwargs: pytest.fail("默认启用不应自己落盘")  # type: ignore[attr-defined]
        monkeypatch.setattr(config_module, "config", fake)

        def no_save(*_args: Any, **_kwargs: Any) -> None:
            pytest.fail("默认启用不应自己落盘")

        monkeypatch.setattr(config_service.ConfigService, "save_config", staticmethod(no_save))
        monkeypatch.setattr(config_module, "save_config", no_save)

        default_on.apply_default_on_plugins_at_startup()

        assert "Akiyo_dayo.message_recall" in fake.PLUGIN_ENABLED
        # 默认启用清单里还有好感自主调整（同一个启动钩子、同一份记录），这里按清单本身比对，
        # 只钉住「钩子改的是内存、不落盘」这件事
        assert fake.PLUGIN_DEFAULT_ON_APPLIED == list(default_on.DEFAULT_ON_BUILTIN_PLUGINS)

    def test_startup_applies_it_before_bootstrap_persists_the_config(self):
        """由 `bootstrap_model_groups()` 那次常规落盘带进配置文件：必须排在它之前"""
        source = (Path(__file__).resolve().parent.parent / "nekro_agent" / "__init__.py").read_text(encoding="utf-8")
        assert "apply_default_on_plugins_at_startup()" in source
        assert source.index("apply_default_on_plugins_at_startup()") < source.index("await bootstrap_model_groups()")
        assert source.index("apply_default_on_plugins_at_startup()") < source.index("await init_plugins()")

    def test_it_runs_before_builtin_plugins_are_loaded(self):
        from nekro_agent.services.plugin.collector import PluginCollector

        source = inspect.getsource(PluginCollector.init_plugins)
        assert "apply_default_on_plugins_at_startup()" in source
        assert source.index("apply_default_on_plugins_at_startup()") < source.index("builtin_plugin_dir.iterdir()")

    def test_default_on_module_has_no_save_call(self):
        """源码级兜底：模块里不出现任何落盘调用"""
        source = (
            Path(__file__).resolve().parent.parent / "nekro_agent" / "services" / "plugin" / "default_on.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not called & {"save_config", "dump_config"}, called

    def test_the_record_is_a_hidden_config_field(self):
        from nekro_agent.core.config import CoreConfig

        field = CoreConfig.model_fields["PLUGIN_DEFAULT_ON_APPLIED"]
        assert field.default == []
        assert (field.json_schema_extra or {}).get("is_hidden") is True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# G. 触发者记录
# ---------------------------------------------------------------------------


class TestRoundTriggerAccounts:
    def test_humans_are_deduped_and_kinds_are_kept(self):
        for trigger in (
            RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT),
            RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT),
            RoundTrigger(kind="timer"),
        ):
            round_trigger_accounts.note_pending_trigger(CHAT_KEY, trigger)

        round_trigger_accounts.begin_round_triggers(CHAT_KEY)

        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == (TRIGGER_ACCOUNT, "timer")

    def test_a_round_takes_the_pending_triggers_and_later_ones_belong_to_the_next(self):
        round_trigger_accounts.note_pending_trigger(CHAT_KEY, RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT))
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)
        # 回合跑起来之后到达的：留在待处理里，不改本轮
        round_trigger_accounts.note_pending_trigger(CHAT_KEY, RoundTrigger(kind="human", sender_id="66601"))
        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == (TRIGGER_ACCOUNT,)

        round_trigger_accounts.clear_round_triggers(CHAT_KEY)
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)
        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == ("66601",)

    def test_empty_and_cleared_read_as_unknown(self):
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)
        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == ("unknown",)

        round_trigger_accounts.note_pending_trigger(CHAT_KEY, RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT))
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)
        round_trigger_accounts.clear_round_triggers(CHAT_KEY)
        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == ("unknown",)

    def test_pending_list_is_capped(self):
        for index in range(round_trigger_accounts.MAX_TRIGGER_ACCOUNTS + 5):
            round_trigger_accounts.note_pending_trigger(CHAT_KEY, RoundTrigger(kind="human", sender_id=f"7{index:04d}"))
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)
        assert len(round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY)) == round_trigger_accounts.MAX_TRIGGER_ACCOUNTS

    def test_channels_do_not_share_records(self):
        round_trigger_accounts.note_pending_trigger(CHAT_KEY, RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT))
        round_trigger_accounts.note_pending_trigger(OTHER_CHAT_KEY, RoundTrigger(kind="timer"))
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)
        round_trigger_accounts.begin_round_triggers(OTHER_CHAT_KEY)

        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == (TRIGGER_ACCOUNT,)
        assert round_trigger_accounts.current_round_trigger_accounts(OTHER_CHAT_KEY) == ("timer",)

    async def test_schedule_records_the_trigger_with_ops_tools_off(self, db, monkeypatch: pytest.MonkeyPatch):
        """默认部署里运维工具总开关是关的：触发者照样要记下来（不借运维的记录簿）"""
        import importlib

        from nekro_agent.models.db_adapter_instance import DBAdapterInstance
        from nekro_agent.services import message_service as message_service_module

        config_module = importlib.import_module("nekro_agent.core.config")
        monkeypatch.setattr(config_module.config, "OPS_AGENT_TOOLS_ENABLED", False)

        async def always_active(*_args: Any, **_kwargs: Any) -> bool:
            return True

        async def not_paused(*_args: Any, **_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(DBAdapterInstance, "is_chat_key_active", always_active)
        monkeypatch.setattr(message_service_module.channel_quota, "is_paused", not_paused)
        service = message_service_module.MessageService()
        # 不让它真的起回合：只看记账
        monkeypatch.setattr(service, "running_tasks", {CHAT_KEY: SimpleNamespace(done=lambda: False)})

        await service.schedule_agent_task(
            chat_key=CHAT_KEY,
            trigger=RoundTrigger(kind="human", sender_id=TRIGGER_ACCOUNT),
        )
        round_trigger_accounts.begin_round_triggers(CHAT_KEY)

        assert round_trigger_accounts.current_round_trigger_accounts(CHAT_KEY) == (TRIGGER_ACCOUNT,)

    def test_run_agent_begins_before_the_round_and_clears_in_finally(self):
        scope_source = inspect.getsource(run_agent_module._run_agent_in_instance_scope)  # noqa: SLF001
        assert "begin_round_triggers(" in scope_source
        assert scope_source.index("begin_round_triggers(") < scope_source.index("await open_ops_round(")
        # 「这一轮是谁叫的 Bot」不借运维工具的触发记录簿：那本子只在 D6 或 SSH 网关开着时才记，
        # 借它的话默认部署里日志永远是 unknown。日志自己的记账模块的代码里不得引用它
        # （按语法树查：模块文档里讲「为什么不借」时会提到这个名字，那不算引用）
        accounts_tree = ast.parse(inspect.getsource(round_trigger_accounts))
        referenced = {node.id for node in ast.walk(accounts_tree) if isinstance(node, ast.Name)} | {
            alias.asname or alias.name
            for node in ast.walk(accounts_tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "round_trigger_book" not in referenced
        # run_agent 用记录簿只为 D6 与 SSH 网关共用的本轮快照（回合开始取一次、收尾清掉），
        # 不在这里另取另看，免得和两家的判定抢同一批触发
        run_agent_source = inspect.getsource(run_agent_module)
        assert "round_trigger_book.take(" not in run_agent_source
        assert "round_trigger_book.peek(" not in run_agent_source
        assert scope_source.index("round_trigger_book.begin_round(") < scope_source.index("await open_ops_round(")

        round_source = inspect.getsource(run_agent_module._run_agent_with_affection_suppression)  # noqa: SLF001
        finally_at = round_source.index("finally:")
        assert "clear_round_triggers(" in round_source[finally_at:]
