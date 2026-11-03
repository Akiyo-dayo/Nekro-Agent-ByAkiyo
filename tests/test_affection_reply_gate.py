"""#71: 好感度软门——低好感用户的非明示触发按概率拦下

好感度参与回复判定，形态是**软门**——低好感降回复概率，
不是硬阈值一刀切。落地形状：

- 只作用于**非明示触发**（`random_chat_check` 随机搭话 / `check_content_trigger`
  关键词命中）；@、私聊、喊名字、`trigger_agent`、`FORCE_TRIGGER` 这类直接召唤
  永远放行——软门管的是环境里的自发参与，直接点名是功能性 UX。
- 乘数表在 `affection/stage.py:_REPLY_EAGERNESS_BANDS`（与阶段阈值对齐）：
  分数 ≥ 0 → 100%（陌生人是正常社交，早期互动不该被"你分低"稀释）、
  提防（-30..-1）→ 50%、厌恶（<-30）→ 10%。恶感段保留非零尾巴——软门是
  "少搭理"不是"永久沉默"，永久沉默走 `is_prevent_trigger`。
- 双层开关：`AFFECTION_ENABLE_SYSTEM`（总闸）∧ `AFFECTION_ENABLE_REPLY_GATE`
  （本特性，频道级可覆盖）。`affection_score_of` 本身 fail-open：读不到按 0 分
  → 意愿 100%，好感度服务挂掉时本门等价于不存在。
- 被拦下的消息**照常落库**——"没看见"是行为层的，不是失忆。
"""

import json
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.config import config as global_config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.stage import (
    AffectionEventSource,
    reply_eagerness,
)
from nekro_agent.services.affection.store import invalidate_cache, set_score
from nekro_agent.services.identity_scope import resolve_identity_scope
from nekro_agent.services.message_service import message_service

ADAPTER = "onebot_v11"
INSTANCE = "gate1"
CHANNEL_ID = "group_888"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{CHANNEL_ID}"
USER_ID = "10001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    invalidate_cache()
    try:
        yield
    finally:
        invalidate_cache()
        await Tortoise.close_connections()


async def seed_channel() -> DBChatChannel:
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name=INSTANCE,
        status="online",
        enabled=True,
        provider="remote_onebot",
        provider_account_id="1000000012",
    )
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=CHANNEL_ID,
        channel_type=ChatType.GROUP.value,
        channel_name="",
        chat_key=CHAT_KEY,
        is_active=True,
        observe_mode=False,
        data=json.dumps({}),
    )


async def seed_score(channel: DBChatChannel, score: int) -> None:
    """按 `affection_score_of` 的同一条解析链落定分数，保证测试读得到"""
    scope = await resolve_identity_scope(channel, USER_ID)
    await set_score(
        AffectionKey.of_scope(scope),
        score=score,
        reason="测试播种",
        source=AffectionEventSource.MANUAL,
        operator="test",
    )
    invalidate_cache()


def human_message(*, is_tome: int = 0, content: str = "在吗在吗") -> ChatMessage:
    return ChatMessage(
        message_id="",
        sender_id=USER_ID,
        sender_name="tester",
        sender_nickname="tester",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        platform_userid=USER_ID,
        is_tome=is_tome,
        is_recalled=False,
        chat_key=CHAT_KEY,
        chat_type=ChatType.GROUP,
        content_text=content,
        content_data=[],
        raw_cq_code="",
        ext_data={},
        send_timestamp=0,
    )


class TestReplyEagerness:
    """乘数表本身：阈值与 `_STAGE_SPECS` 对齐，纯函数不需要库"""

    @pytest.mark.parametrize("score", [0, 9, 10, 30, 60, 100, 200])
    def test_non_negative_scores_pass_through(self, score: int) -> None:
        assert reply_eagerness(score) == 100

    @pytest.mark.parametrize("score", [-1, -15, -29, -30])
    def test_wary_band_halves(self, score: int) -> None:
        assert reply_eagerness(score) == 50

    @pytest.mark.parametrize("score", [-31, -50, -99, -100, -1000])
    def test_hostile_band_keeps_a_nonzero_tail(self, score: int) -> None:
        assert reply_eagerness(score) == 10


class TestReplyGateWiring:
    """软门接线：推一条非明示触发消息，断言调度有没有发生"""

    @pytest.fixture(autouse=True)
    def _quiet_debounce(self, monkeypatch: pytest.MonkeyPatch):
        """防抖换成空转，避免测试真去跑一轮 Agent；断言点是 pending_messages"""

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return

        monkeypatch.setattr(type(message_service), "_debounce_task", noop)
        message_service.pending_messages.pop(CHAT_KEY, None)
        message_service.debounce_timers.pop(CHAT_KEY, None)
        yield
        message_service.pending_messages.pop(CHAT_KEY, None)
        message_service.debounce_timers.pop(CHAT_KEY, None)

    def _patch_effective_config(self, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
        """关键词触发永远命中（`.*`），把"触发没触发"的决定权完全交给软门那一颗骰子"""
        merged = global_config.model_copy(
            update={"AI_CHAT_TRIGGER_REGEX": [".*"], **overrides},
        )

        async def fake_effective(self: DBChatChannel) -> CoreConfig:
            return merged

        monkeypatch.setattr(DBChatChannel, "get_effective_config", fake_effective)

    def _fix_dice(self, monkeypatch: pytest.MonkeyPatch, value: float) -> None:
        monkeypatch.setattr("nekro_agent.services.message_service.random.random", lambda: value)

    async def test_stranger_is_never_gated(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        self._patch_effective_config(monkeypatch)
        self._fix_dice(monkeypatch, 0.999)

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)

        # 没有好感行 → 0 分 → 意愿 100%，再大的骰子也拦不住
        assert CHAT_KEY in message_service.pending_messages

    async def test_hostile_blocked_when_dice_says_no(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_score(channel, -50)
        self._patch_effective_config(monkeypatch)
        self._fix_dice(monkeypatch, 0.99)  # 意愿 10%，99 ≥ 10 → 拦

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)

        assert CHAT_KEY not in message_service.pending_messages

    async def test_hostile_passes_when_dice_says_yes(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_score(channel, -50)
        self._patch_effective_config(monkeypatch)
        self._fix_dice(monkeypatch, 0.01)  # 1 < 10 → 放行

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)

        assert CHAT_KEY in message_service.pending_messages

    async def test_wary_band_is_a_coin_flip(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_score(channel, -15)
        self._patch_effective_config(monkeypatch)
        self._fix_dice(monkeypatch, 0.6)  # 意愿 50%，60 ≥ 50 → 拦

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)
        assert CHAT_KEY not in message_service.pending_messages

        self._fix_dice(monkeypatch, 0.4)  # 40 < 50 → 放行
        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)
        assert CHAT_KEY in message_service.pending_messages

    async def test_explicit_summons_bypasses_gate(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """@Bot 是直接召唤：好感再低也放行，软门只管自发参与"""
        channel = await seed_channel()
        await seed_score(channel, -50)
        self._patch_effective_config(monkeypatch)
        self._fix_dice(monkeypatch, 0.99)

        await message_service.push_human_message(message=human_message(is_tome=1), db_chat_channel=channel)

        assert CHAT_KEY in message_service.pending_messages

    async def test_gate_flag_off_bypasses(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_score(channel, -50)
        self._patch_effective_config(monkeypatch, AFFECTION_ENABLE_REPLY_GATE=False)
        self._fix_dice(monkeypatch, 0.99)

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)

        assert CHAT_KEY in message_service.pending_messages

    async def test_system_off_bypasses(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_score(channel, -50)
        self._patch_effective_config(monkeypatch, AFFECTION_ENABLE_SYSTEM=False)
        self._fix_dice(monkeypatch, 0.99)

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)

        assert CHAT_KEY in message_service.pending_messages

    async def test_gated_message_is_still_recorded(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """"没看见"是行为层的：被拦的消息照常落库，只是不调度回复"""
        channel = await seed_channel()
        await seed_score(channel, -50)
        self._patch_effective_config(monkeypatch)
        self._fix_dice(monkeypatch, 0.99)

        await message_service.push_human_message(message=human_message(), db_chat_channel=channel)

        assert CHAT_KEY not in message_service.pending_messages
        assert await DBChatMessage.filter(chat_key=CHAT_KEY, platform_userid=USER_ID).count() == 1
