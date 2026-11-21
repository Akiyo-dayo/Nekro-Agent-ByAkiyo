"""开场白：判据表的每一行、记录写法、旧初值的清理与 `/reset`

判据表的核心是「记录与当前人设比」：记录相同就什么都不做；键不存在时用频道里有没有
Bot 消息分老频道与新频道；`-1` 与另一个人设 ID 都只记不发。这里逐行钉住，并跑真表确认
`reset_channel` 写 `-1` 时不会丢掉 `data` 里的其它键。

开场白不再发出：发送服务仍然打桩，用来断言「一次都没被调用」；记录照写，写记录时
上一位人设留下的 `<initvar>` 初值被清掉。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.tavern import greeting
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.cleanup_runtime import strip_variable_markup
from nekro_agent.services.tavern.greeting import (
    GREETED_KEY,
    RESET_MARK,
    GreetingDecision,
    decide,
    maybe_greet,
    pick_greeting,
    preview_greeting,
)
from nekro_agent.services.tavern.macros import MacroContext
from nekro_agent.services.tavern.runtime import TavernRound

CHAT_KEY = "onebot_v11-inst1-group_5"
PRIVATE_KEY = "onebot_v11-inst1-private_6"
CODE = "tg0001"
ADAPTER = "onebot_v11"


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _clean_rounds() -> AsyncIterator[None]:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001


async def _preset(name: str = "薄荷") -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content="正文",
        description="",
        tags="",
        author="测试",
    )


async def _card(preset: DBPreset, **overrides: Any) -> DBPresetCard:
    values: dict[str, Any] = {
        "greetings": ["你好，我是{{char}}。"],
        "group_greetings": [],
        "greeting_pick": "first",
        "nickname": "",
    }
    values.update(overrides)
    return await DBPresetCard.create(preset=preset, **values)


async def _channel(
    *,
    chat_key: str = CHAT_KEY,
    channel_type: str = "group",
    data: Optional[dict[str, Any]] = None,
    preset_id: Optional[int] = None,
) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key="inst1",
        channel_id=chat_key.split("-")[-1],
        chat_key=chat_key,
        channel_type=channel_type,
        channel_name="测试",
        data=json.dumps(data or {}, ensure_ascii=False),
        preset_id=preset_id,
        is_active=True,
        observe_mode=False,
    )


async def _bot_message(channel: DBChatChannel) -> None:
    await DBChatMessage.create(
        sender_id="-1",
        sender_name="Bot",
        sender_nickname="Bot",
        is_tome=0,
        is_recalled=False,
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        message_id="bot-m1",
        chat_key=channel.chat_key,
        chat_type=channel.channel_type or "group",
        platform_userid="bot",
        content_text="之前说过的话",
        content_data="[]",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1,
    )


async def _user_message(channel: DBChatChannel, nickname: str) -> None:
    await DBChatMessage.create(
        sender_id="20001",
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        message_id="user-m1",
        chat_key=channel.chat_key,
        chat_type=channel.channel_type or "group",
        platform_userid="20001",
        content_text="在吗",
        content_data="[]",
        raw_cq_code="",
        ext_data="",
        send_timestamp=2,
    )


def _config(**overrides: Any) -> Any:
    values = {
        "TAVERN_COMPAT_ENABLED": True,
        "PERSONA_GREETING_ENABLED": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _tavern(card: Optional[DBPresetCard], *, user_name: str = "阿明", char_name: str = "薄荷") -> TavernRound:
    macro = MacroContext(
        char_name=card.nickname if card is not None and card.nickname else char_name,
        user_name=user_name,
        now=dt.datetime(2026, 1, 1, 12, 0, 0),
        chat_key=CHAT_KEY,
        preset_id=1,
    )
    return TavernRound(
        enabled=True,
        card=card,
        macro=macro,
        persona_content="正文",
        depth_prompt="",
        depth=0,
        auxiliary="",
        post_history="",
        prefill=None,
        slots=None,
    )


class _SendRecorder:
    """发送服务替身：开场白不再发出，用例只断言它一次都没被调用"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def send_recorder(monkeypatch: pytest.MonkeyPatch) -> _SendRecorder:
    recorder = _SendRecorder()

    async def _record(**kwargs: Any) -> None:
        await recorder(**kwargs)

    monkeypatch.setattr(universal_chat_service, "send_agent_message", _record)
    return recorder


class TestDecisionTable:
    async def test_record_is_current_preset_does_nothing(self, db: None, send_recorder: _SendRecorder) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(data={GREETED_KEY: preset.id}, preset_id=preset.id)

        decision = await decide(channel, preset.id, card, _config())

        assert decision == GreetingDecision(send=False, text=None, record=False)
        assert channel._load_data()[GREETED_KEY] == preset.id  # noqa: SLF001
        assert send_recorder.calls == []

    async def test_missing_key_with_bot_message_only_records(self, db: None, send_recorder: _SendRecorder) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)
        await _bot_message(channel)

        decision = await decide(channel, preset.id, card, _config())

        assert decision == GreetingDecision(send=False, text=None, record=True)
        assert send_recorder.calls == []

    async def test_old_channel_greeting_added_later_still_only_records(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset, greetings=["升级之后才补上的开场白"])
        channel = await _channel(preset_id=preset.id)
        await _bot_message(channel)

        decision = await decide(channel, preset.id, card, _config())

        assert decision.send is False
        assert decision.record is True

    async def test_missing_key_without_bot_message_records_without_sending(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)

        decision = await decide(channel, preset.id, card, _config())

        assert decision == GreetingDecision(send=False, text=None, record=True)

    async def test_reset_mark_records_without_sending(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(data={GREETED_KEY: RESET_MARK}, preset_id=preset.id)
        await _bot_message(channel)

        decision = await decide(channel, preset.id, card, _config())

        assert decision == GreetingDecision(send=False, text=None, record=True)

    async def test_other_preset_id_records_without_sending(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(data={GREETED_KEY: 77}, preset_id=preset.id)

        decision = await decide(channel, preset.id, card, _config())

        assert decision == GreetingDecision(send=False, text=None, record=True)

    async def test_greeting_switch_is_deprecated_and_records(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)

        # 开关已废弃：置 False 也照记，不再影响是否发送（发送本来就不会发生）
        decision = await decide(channel, preset.id, card, _config(PERSONA_GREETING_ENABLED=False))

        assert decision == GreetingDecision(send=False, text=None, record=True)

    async def test_no_card_records_but_does_not_send(self, db: None) -> None:
        preset = await _preset()
        channel = await _channel(preset_id=preset.id)

        decision = await decide(channel, preset.id, None, _config())

        assert decision == GreetingDecision(send=False, text=None, record=True)

    async def test_builtin_preset_records_zero(self, db: None) -> None:
        channel = await _channel()

        decision = await decide(channel, 0, None, _config())

        assert decision == GreetingDecision(send=False, text=None, record=True)

    async def test_compat_gate_off_returns_without_any_query(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(data={"custom_channel_name": "旧名"}, preset_id=preset.id)
        queries: list[Any] = []

        def _boom(*args: Any, **_kwargs: Any) -> Any:
            queries.append(args)
            raise AssertionError("闸关时不该查 Bot 消息")

        monkeypatch.setattr(greeting.DBChatMessage, "filter", staticmethod(_boom))

        decision = await decide(channel, preset.id, card, _config(TAVERN_COMPAT_ENABLED=False))

        assert decision == GreetingDecision(send=False, text=None, record=False)
        assert queries == []
        assert channel._load_data() == {"custom_channel_name": "旧名"}  # noqa: SLF001

    async def test_bot_query_only_happens_when_key_is_missing(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset = await _preset()
        card = await _card(preset)
        queries: list[int] = []
        original = greeting.DBChatMessage.filter

        def _counted(*args: Any, **kwargs: Any) -> Any:
            queries.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(greeting.DBChatMessage, "filter", staticmethod(_counted))

        same = await _channel(chat_key="onebot_v11-inst1-group_7", data={GREETED_KEY: preset.id}, preset_id=preset.id)
        switched = await _channel(chat_key="onebot_v11-inst1-group_8", data={GREETED_KEY: 77}, preset_id=preset.id)
        await decide(same, preset.id, card, _config())
        await decide(switched, preset.id, card, _config())
        assert queries == []

        fresh = await _channel(chat_key="onebot_v11-inst1-group_9", preset_id=preset.id)
        await decide(fresh, preset.id, card, _config())
        assert len(queries) == 1


class TestPickGreeting:
    async def test_group_uses_group_greetings_and_private_uses_plain(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset, greetings=["普通开场白"], group_greetings=["群聊专用"])
        group = await _channel(chat_key="onebot_v11-inst1-group_10")
        private = await _channel(chat_key="onebot_v11-inst1-private_11", channel_type="private")

        assert pick_greeting(group, card) == "群聊专用"
        assert pick_greeting(private, card) == "普通开场白"

    async def test_group_falls_back_when_group_list_is_all_empty(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset, greetings=["普通开场白"], group_greetings=["", ""])
        group = await _channel()

        assert pick_greeting(group, card) == "普通开场白"

    async def test_all_empty_means_no_greeting(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset, greetings=["", ""], group_greetings=[])
        group = await _channel()

        assert pick_greeting(group, card) is None

    async def test_random_pick_uses_random_choice(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        preset = await _preset()
        card = await _card(preset, greetings=["第一条", "第二条"], greeting_pick="random")
        group = await _channel()
        seen: list[list[str]] = []

        def _choose(items: list[str]) -> str:
            seen.append(list(items))
            return items[1]

        monkeypatch.setattr(greeting.random, "choice", _choose)

        assert pick_greeting(group, card) == "第二条"
        assert seen == [["第一条", "第二条"]]


class TestGreetingInitvar:
    """开场白里的 `<initvar>`：不再发出，写记录时清掉上一位人设留下的那份"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("你好<initvar>\n时间: 早上\n</initvar>", "时间: 早上"),
            ("<InitVar>a: 1</INITVAR>后文<initvar>b: 2</initvar>", "a: 1"),
            ("< initvar >x: 1</ initvar >", "x: 1"),
            ("<initvar>没闭合", None),
            ("<initvar>  \n </initvar>", None),
            ("没有块", None),
            ("<initvars>a</initvars>", None),
            ('<initvar a=1 b="x">y: 2</initvar>', "y: 2"),
            ("<initvar/>前面是自闭合<initvar>z: 3</initvar>", "z: 3"),
            ("<initvar><initvar>a: 1</initvar></initvar>", None),
        ],
    )
    def test_extract(self, text: str, expected: Optional[str]) -> None:
        assert greeting.extract_initvar_block(text) == expected

    def test_too_long_is_not_kept(self) -> None:
        body = "x" * (greeting.MAX_GREETING_INITVAR_CHARS + 1)

        assert greeting.extract_initvar_block(f"<initvar>{body}</initvar>") is None
        assert greeting.extract_initvar_block(f"<initvar>{body[:-1]}</initvar>") == body[:-1]

    def test_unclosed_flood_is_linear(self) -> None:
        started = time.thread_time()
        greeting.extract_initvar_block("<initvar>" + "< initvar" * 50_000)
        greeting.extract_initvar_block("<" * 200_000)

        assert time.thread_time() - started < 2

    def test_send_path_strips_the_block(self) -> None:
        text = "你好，我是薄荷。\n<initvar>\n时间: 早上\n</initvar>"

        assert strip_variable_markup(text) == "你好，我是薄荷。"
        assert strip_variable_markup("开头<InitVar>a: 1</initvar>结尾") == "开头结尾"

    @pytest.mark.parametrize("text", ["前<initvar a=1>x: 1</initvar>后", "前< InitVar >x: 1</ initvar >后"])
    def test_what_is_stripped_is_also_taken(self, text: str) -> None:
        assert strip_variable_markup(text) == "前后"
        assert greeting.extract_initvar_block(text) == "x: 1"

    async def test_first_round_does_not_write_an_initvar(self, db: None, send_recorder: _SendRecorder) -> None:
        preset = await _preset()
        card = await _card(preset, greetings=["你好，我是{{char}}。<initvar>名字: {{char}}</initvar>"])
        channel = await _channel(preset_id=preset.id)

        sent = await maybe_greet(
            channel=channel,
            preset=preset,
            card=card,
            config=_config(),
            ctx=SimpleNamespace(adapter=object()),
            tavern=_tavern(card),
            one_time_code=CODE,
        )

        assert sent is False
        assert send_recorder.calls == []
        await channel.refresh_from_db()
        assert greeting.GREETING_INITVAR_KEY not in channel._load_data()  # noqa: SLF001

    async def test_switching_preset_clears_the_previous_initvar(self, db: None, send_recorder: _SendRecorder) -> None:
        first_preset = await _preset("薄荷")
        second_preset = await _preset("青柠")
        second_card = await _card(second_preset, greetings=["<initvar>名字: 青柠</initvar>"])
        channel = await _channel(
            preset_id=second_preset.id,
            data={
                GREETED_KEY: first_preset.id,
                greeting.GREETING_INITVAR_KEY: {"preset_id": first_preset.id, "text": "名字: 薄荷"},
            },
        )

        sent = await maybe_greet(
            channel=channel,
            preset=second_preset,
            card=second_card,
            config=_config(),
            ctx=SimpleNamespace(adapter=object()),
            tavern=_tavern(second_card, char_name="青柠"),
            one_time_code=CODE,
        )

        assert sent is False
        assert send_recorder.calls == []
        await channel.refresh_from_db()
        data = channel._load_data()  # noqa: SLF001
        assert greeting.GREETING_INITVAR_KEY not in data
        assert data[GREETED_KEY] == second_preset.id


class TestMaybeGreet:
    async def test_new_channel_records_without_sending(self, db: None, send_recorder: _SendRecorder) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)

        sent = await maybe_greet(
            channel=channel,
            preset=preset,
            card=card,
            config=_config(PERSONA_GREETING_ENABLED=True),
            ctx=SimpleNamespace(adapter=object()),
            tavern=_tavern(card),
            one_time_code=CODE,
        )

        assert sent is False
        assert send_recorder.calls == []
        await channel.refresh_from_db()
        assert channel._load_data()[GREETED_KEY] == preset.id  # noqa: SLF001

    async def test_second_round_same_preset_does_not_repeat(self, db: None, send_recorder: _SendRecorder) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)
        ctx = SimpleNamespace(adapter=object())
        tavern = _tavern(card)

        first = await maybe_greet(
            channel=channel, preset=preset, card=card, config=_config(), ctx=ctx, tavern=tavern, one_time_code=CODE,
        )
        second = await maybe_greet(
            channel=channel, preset=preset, card=card, config=_config(), ctx=ctx, tavern=tavern, one_time_code=CODE,
        )

        assert first is False
        assert second is False
        assert send_recorder.calls == []
        await channel.refresh_from_db()
        assert channel._load_data()[GREETED_KEY] == preset.id  # noqa: SLF001

    async def test_switching_preset_updates_record_without_sending(
        self,
        db: None,
        send_recorder: _SendRecorder,
    ) -> None:
        first_preset = await _preset("薄荷")
        first_card = await _card(first_preset)
        second_preset = await _preset("青柠")
        second_card = await _card(second_preset, greetings=["换人设了"])
        channel = await _channel(preset_id=first_preset.id)
        ctx = SimpleNamespace(adapter=object())

        await maybe_greet(
            channel=channel,
            preset=first_preset,
            card=first_card,
            config=_config(),
            ctx=ctx,
            tavern=_tavern(first_card),
            one_time_code=CODE,
        )
        await maybe_greet(
            channel=channel,
            preset=second_preset,
            card=second_card,
            config=_config(),
            ctx=ctx,
            tavern=_tavern(second_card, char_name="青柠"),
            one_time_code=CODE,
        )

        assert send_recorder.calls == []
        await channel.refresh_from_db()
        assert channel._load_data()[GREETED_KEY] == second_preset.id  # noqa: SLF001

    async def test_greeting_switch_is_deprecated_and_still_writes_record(
        self,
        db: None,
        send_recorder: _SendRecorder,
    ) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)

        sent = await maybe_greet(
            channel=channel,
            preset=preset,
            card=card,
            config=_config(PERSONA_GREETING_ENABLED=False),
            ctx=SimpleNamespace(adapter=object()),
            tavern=_tavern(card),
            one_time_code=CODE,
        )

        assert sent is False
        assert send_recorder.calls == []
        await channel.refresh_from_db()
        assert channel._load_data()[GREETED_KEY] == preset.id  # noqa: SLF001

    async def test_preview_never_sends(self, db: None) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(preset_id=preset.id)

        result = await preview_greeting(
            channel=channel,
            preset=preset,
            card=card,
            config=_config(PERSONA_GREETING_ENABLED=True),
            tavern=_tavern(card),
        )

        assert result == {"will_send": False, "text": ""}


class TestResetChannel:
    async def test_reset_writes_minus_one_and_keeps_other_data(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.schemas.agent_ctx import AgentCtx
        from nekro_agent.services.plugin.collector import plugin_collector

        channel = await _channel(data={"custom_channel_name": "旧名", "auto_disable": {"keep": True}})

        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(plugin_collector, "chat_channel_on_reset", _noop)
        monkeypatch.setattr(AgentCtx, "create_by_chat_key", staticmethod(_noop))

        await channel.reset_channel()
        await channel.refresh_from_db()

        data = channel._load_data()  # noqa: SLF001
        assert data[GREETED_KEY] == RESET_MARK
        assert data["custom_channel_name"] == "旧名"
        assert data["auto_disable"] == {"keep": True}

    async def test_reset_then_next_round_only_records(
        self,
        db: None,
        send_recorder: _SendRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset = await _preset()
        card = await _card(preset)
        channel = await _channel(data={GREETED_KEY: preset.id}, preset_id=preset.id)
        await _bot_message(channel)

        from nekro_agent.schemas.agent_ctx import AgentCtx
        from nekro_agent.services.plugin.collector import plugin_collector

        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(plugin_collector, "chat_channel_on_reset", _noop)
        monkeypatch.setattr(AgentCtx, "create_by_chat_key", staticmethod(_noop))
        await channel.reset_channel()

        sent = await maybe_greet(
            channel=channel,
            preset=preset,
            card=card,
            config=_config(),
            ctx=SimpleNamespace(adapter=object()),
            tavern=_tavern(card),
            one_time_code=CODE,
        )

        assert sent is False
        assert send_recorder.calls == []
        await channel.refresh_from_db()
        assert channel._load_data()[GREETED_KEY] == preset.id  # noqa: SLF001


# 预览与顺序用例只组装提示词，不碰数据库；这里把有副作用的外部步骤换成替身。


class _FakeConfig:
    AI_CHAT_CONTEXT_EXPIRE_SECONDS = 0
    AI_CHAT_CONTEXT_MAX_LENGTH = 10
    AI_SYSTEM_NOTIFY_WINDOW_SIZE = 0
    AI_SYSTEM_NOTIFY_LIMIT = 0
    AI_VISION_IMAGE_LIMIT = 10
    AI_VISION_IMAGE_SIZE_LIMIT_KB = 1024 * 1024
    MEMORY_ENABLE_ENHANCED_RETRIEVAL = False
    AI_ALWAYS_INCLUDE_MSG_ID = False
    AI_CONTEXT_LENGTH_PER_SESSION = 100000
    PROMPT_ADVANCED_SLOTS_ENABLED = True
    USE_MODEL_GROUP = "default"
    TAVERN_COMPAT_ENABLED = True
    PERSONA_GREETING_ENABLED = True
    MODEL_GROUPS = {
        "default": SimpleNamespace(
            CHAT_MODEL="base-model",
            BASE_URL="https://example.invalid/v1",
            ENABLE_VISION=False,
            ENABLE_COT=False,
            PREFILL_MODE="off",
        ),
    }


class _FakeChannel:
    chat_key = CHAT_KEY
    conversation_start_time = dt.datetime(2000, 1, 1)
    workspace_id = None
    adapter_key = ADAPTER
    instance_key = "inst1"
    channel_name = "测试群"
    channel_type = "group"
    chat_type = ChatType.GROUP

    def __init__(self, data: dict[str, Any], preset: Any) -> None:
        self._data = dict(data)
        self._preset = preset
        self.adapter = SimpleNamespace(
            chat_key_rules=["group_<id>"],
            config=SimpleNamespace(SESSION_ENABLE_AT=False),
        )

    def _load_data(self) -> dict[str, Any]:
        return dict(self._data)

    async def get_preset(self) -> Any:
        return self._preset

    async def get_effective_config(self) -> Any:
        return _FakeConfig()

    async def refresh_from_db(self, **_kwargs: Any) -> None:
        return None

    async def save(self, **_kwargs: Any) -> None:
        return None


class _FakeCompiler:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    def render_system_message(self) -> Any:
        return SimpleNamespace(to_dict=lambda: {"content": "system"})

    def render_practice_messages(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def render_history_message(self, **_kwargs: Any) -> Any:
        if _HISTORY_CALLS is not None:
            _HISTORY_CALLS.append("history")
        return SimpleNamespace(to_dict=lambda: {"content": "history"})


_HISTORY_CALLS: Optional[list[str]] = None


async def _install_round_prompt_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tavern: TavernRound,
    history_calls: Optional[list[str]] = None,
) -> Any:
    global _HISTORY_CALLS
    _HISTORY_CALLS = history_calls

    async def _resolve(*_args: Any, **_kwargs: Any) -> TavernRound:
        return tavern

    async def _affection(**_kwargs: Any) -> str:
        return ""

    def _schedule(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _plugins(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(system_prompt="", runtime_prompt="", depth_blocks=[])

    async def _active(**_kwargs: Any) -> list[Any]:
        return []

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _round_worldbook(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            state="skipped",
            dropped=[],
            groups=SimpleNamespace(after_persona=[], before_history=[], at_depth={}),
            budget=None,
            tokens={},
        )

    async def _dialog() -> list[Any]:
        return []

    async def _jinja() -> None:
        return None

    async def _self_info(_chat_key: str) -> Any:
        return SimpleNamespace(platform_name="qq", user_id="10001")

    async def _runtime_prompt() -> str:
        return ""

    adapter = SimpleNamespace(
        set_dialog_example=_dialog,
        get_jinja_env=_jinja,
        get_self_info=_self_info,
        render_runtime_prompt=_runtime_prompt,
    )
    ctx = SimpleNamespace(adapter=adapter, from_chat_key=CHAT_KEY)

    monkeypatch.setattr(round_prompt_module.tavern_runtime, "resolve_tavern_round", _resolve)
    monkeypatch.setattr(
        round_prompt_module.tavern_runtime,
        "expander",
        lambda _code, **_kwargs: (lambda text: text),
    )
    monkeypatch.setattr(round_prompt_module.wb_runtime, "resolve_round", _round_worldbook)
    monkeypatch.setattr(round_prompt_module.wb_render, "render_block", lambda *_a, **_k: "")
    monkeypatch.setattr(round_prompt_module.wb_render, "rendered_items", lambda *_a, **_k: [])
    monkeypatch.setattr(round_prompt_module, "render_affection_prompt", _affection)
    monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", _schedule)
    monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", _plugins)
    monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", _active)
    monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", _noop)
    monkeypatch.setattr(round_prompt_module, "render_ops_prompt_section", lambda _ctx: "")
    monkeypatch.setattr(round_prompt_module, "PromptCompiler", _FakeCompiler)
    return ctx


class TestRoundPrompt:
    async def test_dry_run_reports_no_greeting_without_writing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        send_recorder: _SendRecorder,
    ) -> None:
        preset = SimpleNamespace(id=1, name="薄荷", content="正文")
        card = SimpleNamespace(
            nickname="",
            greetings=["{{user}}，欢迎"],
            group_greetings=[],
            greeting_pick="first",
        )
        channel = _FakeChannel({GREETED_KEY: RESET_MARK}, preset)
        tavern = _tavern(card)  # type: ignore[arg-type]
        ctx = await _install_round_prompt_fakes(monkeypatch, tavern=tavern)

        result = await round_prompt_module.build_round_prompt(
            chat_key=CHAT_KEY,
            db_chat_channel=channel,  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
            one_time_code=CODE,
            trigger_message=SimpleNamespace(sender_nickname="阿明"),
            dry_run=True,
        )

        assert result.meta["greeting"] == {"will_send": False, "text": ""}
        assert channel._load_data()[GREETED_KEY] == RESET_MARK  # noqa: SLF001
        assert send_recorder.calls == []

    async def test_greeting_check_runs_before_history_is_rendered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        order: list[str] = []
        preset = SimpleNamespace(id=1, name="薄荷", content="正文")
        card = SimpleNamespace(
            nickname="",
            greetings=["你好"],
            group_greetings=[],
            greeting_pick="first",
        )
        channel = _FakeChannel({GREETED_KEY: RESET_MARK}, preset)
        tavern = _tavern(card)  # type: ignore[arg-type]
        ctx = await _install_round_prompt_fakes(monkeypatch, tavern=tavern, history_calls=order)

        async def _fake_greet(**_kwargs: Any) -> bool:
            order.append("greet")
            return False

        monkeypatch.setattr(round_prompt_module.tavern_greeting, "maybe_greet", _fake_greet)

        await round_prompt_module.build_round_prompt(
            chat_key=CHAT_KEY,
            db_chat_channel=channel,  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
            one_time_code=CODE,
            trigger_message=SimpleNamespace(sender_nickname="阿明"),
            dry_run=False,
        )

        assert order == ["greet", "history"]


class TestConfigDefault:
    def test_tavern_gate_defaults(self) -> None:
        assert config.TAVERN_COMPAT_ENABLED is True
        assert config.PERSONA_GREETING_ENABLED is False
