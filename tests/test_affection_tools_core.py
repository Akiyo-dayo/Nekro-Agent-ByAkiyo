"""好感度工具入口在存储层与评估层的三条契约

主模型现在可以在对话里自己动这一行（「好感自主调整」写 `source=tool` 的流水，改称呼也走同
一个来源）。这一份钉的是宿主怎么认这笔账：

- 额度口径：滚动窗口只数模型写的分，`llm` 与 `tool` 必须共用同一份日 / 周预算，
  运营动作（人工调整、重置、合并补偿）一分都不占；
- 不重复计分：同一段对话里已经被工具调过的人，后台评估不能再判一次；
- 称呼的写入口：分数与互动痕迹一概不动，NUL / 换行 / 超长由入口统一清洗，
  三种「顺带清描述」的路径各自保留或清除称呼的语义要分清。

夹具范式照抄 `tests/test_affection_milestones_flow.py`：内存 SQLite 建真表、频道用替身、
评估模型用固定返回的替身。不另造一套的理由是同一份判据只能有一处实现，测试里的
「等价替身」迟早会与真实现分叉。
"""

import time
from datetime import timedelta
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.evaluator import evaluate_channel, reset_evaluation_throttle
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.merge import merge_accounts
from nekro_agent.services.affection.stage import (
    MODEL_SOURCES,
    AffectionEventSource,
    AffectionWindowUsage,
)
from nekro_agent.services.affection.store import (
    MAX_CALL_NAME_LENGTH,
    AffectionSnapshot,
    apply_delta,
    clear_narrative,
    edit_narrative,
    invalidate_cache,
    load_snapshot,
    set_call_name,
    set_score,
    tool_adjusted_since,
    window_usage,
)
from nekro_agent.services.identity_scope import (
    PRESET_NONE,
    ScopeDomain,
    resolve_identity_scope,
)

ADAPTER_KEY = "onebot_v11"
INSTANCE_KEY = "ob24680"
USER_ID = "24680"
OTHER_ID = "13579"
ALT_ACCOUNT = "24680-alt"
NICKNAME = "小明"
OTHER_NICKNAME = "小红"
CHAT_KEY = f"{ADAPTER_KEY}-{INSTANCE_KEY}-group_4242"
OTHER_CHAT_KEY = f"{ADAPTER_KEY}-{INSTANCE_KEY}-group_5353"

# 理由文案由宿主固定，用例按原文核对：它会被后台时间线直接展示
REASON_CALL_NAME = "改了称呼"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def core_db():
    """内存 SQLite 上的真实表结构

    额度、流水与称呼都是数据层的性质，用假存储验证等于把要保护的东西替换掉了。
    """
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["nekro_agent.models"]},
    )
    await Tortoise.generate_schemas()
    invalidate_cache()
    reset_evaluation_throttle()
    try:
        yield
    finally:
        invalidate_cache()
        reset_evaluation_throttle()
        await Tortoise.close_connections()


def make_config(**overrides: Any) -> CoreConfig:
    """启用好感度的一份配置

    里程碑默认关掉：这一份用例关心的是分数与流水，不关心跨门槛时写了什么记忆，
    关掉能少拖进记忆层的几张表与向量替身。
    """
    base: Dict[str, Any] = {
        "AFFECTION_ENABLE_SYSTEM": True,
        "AFFECTION_MAX_DELTA_PER_ROUND": 5,
        "AFFECTION_UPDATE_MIN_INTERVAL_SECONDS": 0,
        "AFFECTION_CACHE_TTL_SECONDS": 0,
        "AFFECTION_MILESTONE_ENABLED": False,
        "AI_CHAT_CONTEXT_EXPIRE_SECONDS": 1800,
    }
    base.update(overrides)
    return config.model_copy(update=base)


class _StubChannel:
    """频道替身：解析器要的三样 + 评估要的会话标识与会话类型"""

    def __init__(self, chat_key: str = CHAT_KEY, *, preset_id: int = PRESET_NONE) -> None:
        self.chat_key = chat_key
        self.adapter_key = ADAPTER_KEY
        self.instance_key = INSTANCE_KEY
        self.channel_type = "group"
        self._preset_id = preset_id

    @property
    def chat_type(self) -> ChatType:
        return ChatType(self.channel_type)

    async def get_effective_preset_id(self) -> int:
        return self._preset_id

    async def get_preset(self):
        return type("_Preset", (), {"name": "测试人设", "content": "你是一只话很多的猫。"})()


async def add_message(
    content: str,
    *,
    platform_userid: str = USER_ID,
    nickname: str = NICKNAME,
    chat_key: str = CHAT_KEY,
    timestamp: int,
) -> DBChatMessage:
    """往频道里塞一条真人消息；时间由调用方给，好让「这段对话的起点」可预期"""
    return await DBChatMessage.create(
        sender_id=platform_userid,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=1,
        is_recalled=False,
        adapter_key=ADAPTER_KEY,
        instance_key=INSTANCE_KEY,
        message_id=f"m{platform_userid}{timestamp}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=platform_userid,
        content_text=content,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=timestamp,
    )


def stub_llm(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """让评估链路里的那次模型调用返回固定结果"""
    import json

    class _Response:
        response_content = payload if isinstance(payload, str) else json.dumps(payload)

    async def _fake_gen(**_kwargs: Any) -> Any:
        return _Response()

    from nekro_agent.services.agent import openai as openai_module

    monkeypatch.setattr(openai_module, "gen_openai_chat_response", _fake_gen)


def verdicts_of(*items: Dict[str, Any]) -> Dict[str, Any]:
    """评估结果的外壳；每个元素按人给一条"""
    return {"users": list(items)}


async def resolve_key(channel: _StubChannel, platform_userid: str) -> AffectionKey:
    """按「某人在某个频道说话」解析好感键——与热路径完全同一条链路"""
    return AffectionKey.of_scope(await resolve_identity_scope(channel, platform_userid))  # type: ignore[arg-type]


async def prepare() -> tuple[_StubChannel, CoreConfig, AffectionKey]:
    channel = _StubChannel()
    cfg = make_config()
    return channel, cfg, await resolve_key(channel, USER_ID)


async def shift_last_event_back(platform_userid: str, *, minutes: int = 60) -> None:
    """把这个人最近一条流水的 `create_time` 往前挪

    改数据比替时钟可靠：`db_now()` 在多个模块里被各自 import，替一处就会漏一处，
    而判据读的正是流水上那个真实的 `create_time`。
    """
    event = await DBAffectionEvent.filter(platform_userid=platform_userid).order_by("-id").first()
    assert event is not None
    await DBAffectionEvent.filter(id=event.id).update(create_time=db_now() - timedelta(minutes=minutes))


async def nonzero_llm_deltas(platform_userid: str) -> List[int]:
    """这个人名下全部非零的 `llm` 变化量，按落库顺序

    评估「不加分」与「照常加分」的差别只在这里：`delta=0` 的那条流水是照常写的
    （它带着新的印象），所以不能拿「有没有流水」当判据。
    """
    rows = (
        DBAffectionEvent.filter(platform_userid=platform_userid, source=AffectionEventSource.LLM.value)
        .exclude(delta=0)
        .order_by("id")
        .values_list("delta", flat=True)
    )
    return [int(delta) for delta in await rows]


# ---------------------------------------------------------------------------
# 一、模型来源的口径
# ---------------------------------------------------------------------------


class TestModelSources:
    def test_model_sources_are_exactly_llm_and_tool(self) -> None:
        """额度和里程碑都按这一份集合判定，多一个少一个都是口径漂移

        多一个（人工调整）会让管理员的动作占掉用户额度；少一个（工具调分）会让
        「评估一次、模型自己再调一次」把同一段对话的额度翻倍。
        """
        assert MODEL_SOURCES == frozenset({AffectionEventSource.LLM, AffectionEventSource.TOOL})
        assert {source.value for source in MODEL_SOURCES} == {"llm", "tool"}


# ---------------------------------------------------------------------------
# 二、额度口径
# ---------------------------------------------------------------------------


class TestWindowUsage:
    async def test_llm_and_tool_share_one_budget(self, core_db: None) -> None:
        """两种模型来源共用一个窗口：涨跌都计入，跌幅记绝对值"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await apply_delta(key, delta=6, reason="评估给的", source=AffectionEventSource.LLM, chat_key=CHAT_KEY)
        await apply_delta(key, delta=4, reason="主模型当场给的", source=AffectionEventSource.TOOL, chat_key=CHAT_KEY)
        await apply_delta(key, delta=-2, reason="评估扣的", source=AffectionEventSource.LLM, chat_key=CHAT_KEY)
        await apply_delta(key, delta=-3, reason="主模型当场扣的", source=AffectionEventSource.TOOL, chat_key=CHAT_KEY)

        assert await window_usage(key) == AffectionWindowUsage(daily_gain=10, daily_drop=5, weekly_gain=10)

    async def test_operator_sources_do_not_consume_budget(self, core_db: None) -> None:
        """人工调整、重置、合并补偿都不占额度，也不会把额度「退」回来

        它们改的是数据而不是关系：管理员修一次分不该顺手花掉用户的日额度，
        也不该因为把分调低而让刷分的人白拿一份新预算。
        """
        key = await resolve_key(_StubChannel(), USER_ID)
        await apply_delta(key, delta=7, reason="活动奖励", source=AffectionEventSource.MANUAL, operator="admin")
        await set_score(key, score=0, reason="管理员重置", source=AffectionEventSource.RESET, operator="admin")
        await apply_delta(key, delta=5, reason="并入另一个号的更高好感", source=AffectionEventSource.MERGE)

        assert await window_usage(key) == AffectionWindowUsage.unused()

    async def test_tool_events_on_a_merged_alias_are_counted(self, core_db: None) -> None:
        """合并过的副号上的工具流水也要计入

        额度按「人」算：并集若只看主账号，换个号让模型自己调一次就能再拿一份额度。
        """
        channel = _StubChannel()
        main_key = await resolve_key(channel, USER_ID)
        alt_key = await resolve_key(channel, ALT_ACCOUNT)  # 还没有别名，此刻它自成一个「人」
        await apply_delta(alt_key, delta=5, reason="副号这边主模型自己调的分", source=AffectionEventSource.TOOL)
        assert (await window_usage(main_key)).daily_gain == 0

        await merge_accounts(
            ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_KEY),
            person_uid=USER_ID,
            alias_platform_userid=ALT_ACCOUNT,
        )

        # 5；合并补偿那一笔是 `merge` 来源，不占额度
        assert (await window_usage(main_key)).daily_gain == 5


# ---------------------------------------------------------------------------
# 三、工具调分的判定
# ---------------------------------------------------------------------------


class TestToolAdjustedSince:
    async def test_same_channel_after_since_is_true(self, core_db: None) -> None:
        key = await resolve_key(_StubChannel(), USER_ID)
        await apply_delta(key, delta=3, reason="主模型当场调分", source=AffectionEventSource.TOOL, chat_key=CHAT_KEY)

        assert await tool_adjusted_since(key, chat_key=CHAT_KEY, since=db_now() - timedelta(minutes=5)) is True

    async def test_other_channel_is_false(self, core_db: None) -> None:
        """别的频道里的调分不是「这段对话已经表过态」"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await apply_delta(key, delta=3, reason="在另一个群里调的", source=AffectionEventSource.TOOL, chat_key=OTHER_CHAT_KEY)

        assert await tool_adjusted_since(key, chat_key=CHAT_KEY, since=db_now() - timedelta(minutes=5)) is False

    async def test_before_since_is_false(self, core_db: None) -> None:
        """早于这段对话起点的调分属于上一段对话，评估该照常打分"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await apply_delta(key, delta=3, reason="很久以前调的", source=AffectionEventSource.TOOL, chat_key=CHAT_KEY)
        await shift_last_event_back(USER_ID)

        assert await tool_adjusted_since(key, chat_key=CHAT_KEY, since=db_now() - timedelta(minutes=5)) is False

    async def test_call_name_only_is_false(self, core_db: None) -> None:
        """改称呼那条 `delta=0` 的流水没有动分数，评估该照常打分"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await set_call_name(key, call_name="阿明", chat_key=CHAT_KEY)

        assert await tool_adjusted_since(key, chat_key=CHAT_KEY, since=db_now() - timedelta(minutes=5)) is False

    async def test_llm_only_is_false(self, core_db: None) -> None:
        """`llm` 流水不是主模型自己的表态：那正是评估写下的，不能拿它挡评估"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await apply_delta(key, delta=3, reason="上一轮评估给的", source=AffectionEventSource.LLM, chat_key=CHAT_KEY)

        assert await tool_adjusted_since(key, chat_key=CHAT_KEY, since=db_now() - timedelta(minutes=5)) is False


# ---------------------------------------------------------------------------
# 四、评估不重复计分
# ---------------------------------------------------------------------------


class TestEvaluationDoesNotCountTwice:
    async def test_tool_adjusted_person_keeps_score_but_takes_new_feeling(
        self,
        core_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """这段对话里已经被工具调过：不再加分，但新印象照写

        「不加分」不等于「什么都不写」：`feeling` 是这一轮对这个人最新的观察，
        丢掉它等于这一轮白跑。
        """
        channel, cfg, key = await prepare()
        now = int(time.time())
        await add_message("今天被当众说了一顿，你陪我把话说完才走", timestamp=now - 120)
        await apply_delta(
            key,
            delta=3,
            reason="主模型当场给了分",
            source=AffectionEventSource.TOOL,
            chat_key=CHAT_KEY,
            state_text="旧印象：他有点闷",
        )
        assert (await load_snapshot(key, ttl_seconds=0)).state_text == "旧印象：他有点闷"
        stub_llm(
            monkeypatch,
            verdicts_of({"user_id": USER_ID, "delta": 4, "reason": "他愿意把话听完", "feeling": "这个人说话让人踏实"}),
        )

        assert await evaluate_channel(channel, cfg) == 1  # type: ignore[arg-type]

        snapshot = await load_snapshot(key, ttl_seconds=0)
        assert snapshot.score == 3  # 工具那一笔还在，评估没有再 +4
        assert snapshot.state_text == "这个人说话让人踏实"
        assert list(await nonzero_llm_deltas(USER_ID)) == []

    async def test_other_person_in_the_same_round_still_gains(
        self,
        core_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """挡的只该是「被工具调过的那个人」，同轮里的其他人照常加分"""
        channel, cfg, key = await prepare()
        now = int(time.time())
        await add_message("今天谢谢你陪我把话说完", timestamp=now - 120)
        await add_message("我也在，顺手把桌子收了", platform_userid=OTHER_ID, nickname=OTHER_NICKNAME, timestamp=now - 100)
        await apply_delta(key, delta=3, reason="主模型当场给了分", source=AffectionEventSource.TOOL, chat_key=CHAT_KEY)
        stub_llm(
            monkeypatch,
            verdicts_of(
                {"user_id": USER_ID, "delta": 4, "reason": "他愿意把话听完", "feeling": "这个人说话让人踏实"},
                {"user_id": OTHER_ID, "delta": 4, "reason": "她顺手收拾了桌子", "feeling": "她挺会照顾人"},
            ),
        )

        assert await evaluate_channel(channel, cfg) == 2  # type: ignore[arg-type]

        assert (await load_snapshot(key, ttl_seconds=0)).score == 3
        other_key = await resolve_key(channel, OTHER_ID)
        assert (await load_snapshot(other_key, ttl_seconds=0)).score == 4
        assert list(await nonzero_llm_deltas(OTHER_ID)) == [4]

    async def test_tool_adjust_in_another_channel_still_gains(
        self,
        core_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """别的频道里调过分不影响本频道的评估"""
        channel, cfg, key = await prepare()
        now = int(time.time())
        await add_message("今天谢谢你陪我把话说完", timestamp=now - 120)
        await apply_delta(key, delta=3, reason="在另一个群里调的", source=AffectionEventSource.TOOL, chat_key=OTHER_CHAT_KEY)
        stub_llm(
            monkeypatch,
            verdicts_of({"user_id": USER_ID, "delta": 4, "reason": "他愿意把话听完", "feeling": "这个人说话让人踏实"}),
        )

        assert await evaluate_channel(channel, cfg) == 1  # type: ignore[arg-type]

        assert (await load_snapshot(key, ttl_seconds=0)).score == 7
        assert list(await nonzero_llm_deltas(USER_ID)) == [4]

    async def test_tool_adjust_before_the_transcript_still_gains(
        self,
        core_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """早于这段对话最早那条消息的调分属于上一段，不该挡住这一轮"""
        channel, cfg, key = await prepare()
        now = int(time.time())
        await add_message("今天谢谢你陪我把话说完", timestamp=now - 120)
        await apply_delta(key, delta=3, reason="上一段对话里调的", source=AffectionEventSource.TOOL, chat_key=CHAT_KEY)
        await shift_last_event_back(USER_ID)
        stub_llm(
            monkeypatch,
            verdicts_of({"user_id": USER_ID, "delta": 4, "reason": "他愿意把话听完", "feeling": "这个人说话让人踏实"}),
        )

        assert await evaluate_channel(channel, cfg) == 1  # type: ignore[arg-type]

        assert (await load_snapshot(key, ttl_seconds=0)).score == 7
        assert list(await nonzero_llm_deltas(USER_ID)) == [4]

    async def test_call_name_only_still_gains(
        self,
        core_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """只改过称呼：那一条 `delta=0`，评估照常打分，称呼也不被评估覆盖"""
        channel, cfg, key = await prepare()
        now = int(time.time())
        await add_message("今天谢谢你陪我把话说完", timestamp=now - 120)
        await set_call_name(key, call_name="阿明", chat_key=CHAT_KEY)
        stub_llm(
            monkeypatch,
            verdicts_of({"user_id": USER_ID, "delta": 4, "reason": "他愿意把话听完", "feeling": "这个人说话让人踏实"}),
        )

        assert await evaluate_channel(channel, cfg) == 1  # type: ignore[arg-type]

        snapshot = await load_snapshot(key, ttl_seconds=0)
        assert snapshot.score == 4
        assert snapshot.call_name == "阿明"
        assert list(await nonzero_llm_deltas(USER_ID)) == [4]


# ---------------------------------------------------------------------------
# 五、称呼的写入口
# ---------------------------------------------------------------------------


class TestCallNameWrites:
    async def test_set_call_name_leaves_score_and_interaction_alone(self, core_db: None) -> None:
        """改称呼不是一次互动：分数、两段描述、互动次数与最近互动时间都不动

        `last_interact_at` 尤其要紧：久别提示按它算，记个称呼就让「好久不见」消失，
        等于用一句昵称抹掉真正的时间感。
        """
        channel = _StubChannel()
        key = await resolve_key(channel, USER_ID)
        seed = await apply_delta(
            key,
            delta=5,
            reason="聊得不错",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
            state_text="他挺有意思",
            event_text="聊了很久",
        )
        assert seed.last_interact_at is not None

        snapshot = await set_call_name(key, call_name="阿明", chat_key=CHAT_KEY, nickname=NICKNAME)

        assert snapshot.call_name == "阿明"
        assert snapshot.score == seed.score
        assert snapshot.stage == seed.stage
        assert snapshot.state_text == seed.state_text
        assert snapshot.last_event_text == seed.last_event_text
        assert snapshot.interaction_count == seed.interaction_count
        assert snapshot.last_interact_at == seed.last_interact_at

        record = await DBAffection.get(**key.as_columns())
        assert record.call_name == "阿明"
        assert record.last_interact_at == seed.last_interact_at

        event = await DBAffectionEvent.filter(platform_userid=USER_ID, source=AffectionEventSource.TOOL.value).first()
        assert event is not None
        assert (event.source, event.delta, event.reason, event.chat_key) == (
            AffectionEventSource.TOOL.value,
            0,
            REASON_CALL_NAME,
            CHAT_KEY,
        )

    async def test_call_name_is_cleaned_and_truncated(self, core_db: None) -> None:
        """NUL、换行、超长都在入口收掉：这一列会被下一轮提示词原样回注"""
        key = await resolve_key(_StubChannel(), USER_ID)

        cleaned = await set_call_name(key, call_name="阿\x00明\n同学")
        assert cleaned.call_name == "阿明 同学"
        assert "\x00" not in cleaned.call_name
        assert "\n" not in cleaned.call_name

        long_name = "小" * (MAX_CALL_NAME_LENGTH + 6)
        truncated = await set_call_name(key, call_name=long_name)
        assert truncated.call_name != long_name
        assert truncated.call_name.startswith("小" * MAX_CALL_NAME_LENGTH)
        assert len(truncated.call_name) <= MAX_CALL_NAME_LENGTH + 1

    async def test_clear_narrative_keeps_call_name_unless_asked(self, core_db: None) -> None:
        """评估被拦后的清理不碰称呼；管理员「只清印象」才连称呼一起清

        称呼不在评估的提示词里，也就不是嫌疑内容，没有理由跟着一起丢。
        """
        key = await resolve_key(_StubChannel(), USER_ID)
        await set_call_name(key, call_name="阿明")

        kept = await clear_narrative(
            key,
            source=AffectionEventSource.BLOCKED_CLEAR,
            reason="评估被拦后的清理",
        )
        assert kept.call_name == "阿明"

        cleared = await clear_narrative(
            key,
            source=AffectionEventSource.NARRATIVE_CLEAR,
            reason="管理员清空了印象",
            clear_call_name=True,
        )
        assert cleared.call_name == ""

    async def test_set_score_clears_call_name_only_on_reset(self, core_db: None) -> None:
        """重置回到「这个人我一无所知」，人工调分只改分数"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await set_call_name(key, call_name="阿明")

        adjusted = await set_score(key, score=12, reason="管理员调分", source=AffectionEventSource.MANUAL)
        assert adjusted.call_name == "阿明"

        reset = await set_score(
            key,
            score=0,
            reason="管理员重置",
            source=AffectionEventSource.RESET,
            clear_state_text=True,
        )
        assert reset.call_name == ""
        assert reset.state_text == ""
        assert reset.last_event_text == ""

    async def test_edit_narrative_call_name_none_means_leave_it(self, core_db: None) -> None:
        """None 是「没传这一栏」，空串才是「清掉」——两者不能兼职"""
        key = await resolve_key(_StubChannel(), USER_ID)
        await set_call_name(key, call_name="阿明")

        kept = await edit_narrative(key, state_text="新印象", last_event_text="最近没什么事", operator="admin")
        assert kept.call_name == "阿明"

        changed = await edit_narrative(
            key,
            state_text="新印象",
            last_event_text="最近没什么事",
            operator="admin",
            call_name="小明",
        )
        assert changed.call_name == "小明"

        cleared = await edit_narrative(
            key,
            state_text="新印象",
            last_event_text="最近没什么事",
            operator="admin",
            call_name="",
        )
        assert cleared.call_name == ""


# ---------------------------------------------------------------------------
# 六、快照带出这两项
# ---------------------------------------------------------------------------


class TestSnapshotFields:
    async def test_from_record_carries_call_name_and_last_interact(self, core_db: None) -> None:
        key = await resolve_key(_StubChannel(), USER_ID)
        written = await apply_delta(
            key,
            delta=5,
            reason="聊得不错",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
        )
        await set_call_name(key, call_name="阿明")
        record = await DBAffection.get(**key.as_columns())

        snapshot = AffectionSnapshot.from_record(record)
        assert snapshot.call_name == "阿明"
        assert record.last_interact_at is not None
        assert snapshot.last_interact_at == record.last_interact_at == written.last_interact_at

    def test_empty_has_no_call_name_and_no_interact_time(self) -> None:
        """空快照代表「从来没打过交道」：称呼是空串，互动时间是 None"""
        key = AffectionKey(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_KEY,
            domain_group_id=0,
            person_uid=USER_ID,
            preset_id=PRESET_NONE,
        )
        empty = AffectionSnapshot.empty(key)
        assert empty.call_name == ""
        assert empty.last_interact_at is None
        assert empty.exists is False
