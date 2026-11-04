"""好感度的「记忆」删不掉、盖不住、撞甲后清不掉——复现与现状钉子

## 好感度这一侧的「记忆」是什么

不是长期记忆库（`mem_*` 五张表，工作区记忆页能逐条编辑 / 删除），而是好感行上的两段叙述：
`DBAffection.state_text`（提示词里的 `feeling:`）与 `last_event_text`（`recently:`）。
它们由后台评估模型写，每一轮按「域 / 人 / 人设」读出来、包成不可信块注入主对话的系统提示词；
评估模型下一轮又把当前的 `state_text` 当作 `current:` 喂回给自己。

## 两类用例

- **现状钉子**：覆盖是「改原行、不追加」；空 feeling 保留旧描述；后台重置当场生效；
  流水里的原因叙述不进提示词；同一域同一人设下一份描述跟着人走遍所有频道（撞甲的波及面）；
  `/reset` 不清好感、不改分数。这些是设计如此的事，钉住是为了改动时看得见改了什么。
- **曾经的缺口，已修**（原先是 `xfail(strict=True)`）：撞甲后评估被上游拦下、描述永远盖不掉；
  `/reset` 之后评估与注入仍读重置前的消息；后台「重置」之后下一轮评估从同一段旧对话里把它写回来；
  重置与进行中的评估赛跑时评估赢。修法的分支用例在 `tests/test_affection_narrative_fixes.py`。

「撞甲」内容在用例里用占位串 `POISON_MARK` 代替；上游审核用一个看见它就抛
`LLMContentBlockedError` 的假模型模拟——上游以 `finish_reason=content_filter / safety`
拒绝时，`gen_openai_chat_response` 抛的正是这个异常。
"""

import json
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

import pytest
from tortoise.timezone import now as db_now

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.services.affection import evaluator as evaluator_module
from nekro_agent.services.affection.evaluator import _run_evaluation, evaluate_channel
from nekro_agent.services.affection.prompt import render_affection_prompt
from nekro_agent.services.affection.stage import AffectionEventSource
from nekro_agent.services.affection.store import apply_delta, list_events, load_snapshot, set_score
from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent.openai import LLMContentBlockedError
from tests.test_affection_system import (
    CHAT_KEY_A,
    CHAT_KEY_B,
    INSTANCE_A,
    INSTANCE_B,
    KEY_A,
    ONE_TIME_CODE,
    USER_ID,
    _StubChannel,
    add_message,
    affection_db,  # noqa: F401  # pytest 固件靠导入生效
    make_config,
    zero_config_key,
)

POISON_MARK = "POISONMARK7F3"
OTHER_USER = "654321"
PRIVATE_CHAT_KEY_A = f"onebot_v11-{INSTANCE_A}-private_{USER_ID}"
PRESET_OTHER = 7


class _ResettableChannel(_StubChannel):
    """带 `conversation_start_time` 的频道替身——`/reset` 只改这一列（`DBChatChannel.reset_channel`）"""

    def __init__(self, *args: Any, conversation_start_time: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.conversation_start_time = conversation_start_time


class _FakeUpstream:
    """假的评估模型上游

    记下每次评估请求的用户提示词；`blocks=True` 时提示词里一出现撞甲标记就像真上游那样
    拒绝受理（抛 `LLMContentBlockedError`），否则按 `verdict_for(提示词)` 回一段 JSON。
    """

    def __init__(self, verdict_for: Callable[[str], Dict[str, Any]], *, blocks: bool) -> None:
        self.verdict_for = verdict_for
        self.blocks = blocks
        self.prompts: List[str] = []
        self.before_reply: Optional[Callable[[], Any]] = None

    async def __call__(self, **kwargs: Any) -> Any:
        user_prompt = kwargs["messages"][1]["content"]
        self.prompts.append(user_prompt)
        if self.blocks and POISON_MARK in user_prompt:
            raise LLMContentBlockedError("content_filter", stream_mode=False)
        if self.before_reply is not None:
            await self.before_reply()
        content = json.dumps(self.verdict_for(user_prompt), ensure_ascii=False)
        return type("_Response", (), {"response_content": content})()


def install_upstream(monkeypatch: pytest.MonkeyPatch, upstream: _FakeUpstream) -> _FakeUpstream:
    monkeypatch.setattr(openai_module, "gen_openai_chat_response", upstream)
    return upstream


def plain_verdict(feeling: str, *, delta: int = 1, reason: str = "聊了几句") -> Callable[[str], Dict[str, Any]]:
    return lambda _prompt: {"users": [{"user_id": USER_ID, "delta": delta, "reason": reason, "feeling": feeling}]}


async def poison_the_narrative() -> None:
    """模拟「撞甲内容已经被写进好感描述」这个起点"""
    await apply_delta(
        KEY_A,
        delta=5,
        reason=f"他又提起{POISON_MARK}",
        source=AffectionEventSource.LLM,
        state_text=f"他总拿{POISON_MARK}开玩笑，我有点招架不住。",
        event_text=f"他又提起{POISON_MARK}",
    )


async def render(channel: Any) -> str:
    return await render_affection_prompt(
        db_chat_channel=channel,
        one_time_code=ONE_TIME_CODE,
        config=make_config(),
    )


def now_ts() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# 现状钉子
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("affection_db")
class TestOverwriteSemantics:
    """覆盖：同一个人的描述是改原行，不追加；流水才是追加"""

    async def test_new_feeling_overwrites_the_same_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now_ts() - 120)

        install_upstream(monkeypatch, _FakeUpstream(plain_verdict("第一印象：话多。"), blocks=False))
        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]
        install_upstream(monkeypatch, _FakeUpstream(plain_verdict("第二印象：其实挺细心。"), blocks=False))
        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        assert await DBAffection.filter(platform_userid=USER_ID).count() == 1
        assert (await load_snapshot(KEY_A, ttl_seconds=0)).state_text == "第二印象：其实挺细心。"
        rendered = await render(channel)
        assert "第二印象" in rendered
        assert "第一印象" not in rendered
        # 流水是追加的：两次评估两条事件
        assert len(await list_events(KEY_A)) == 2

    async def test_empty_feeling_keeps_the_old_narrative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模型回空 feeling 时旧描述原样保留（`state_text=feeling or None`）；delta 也是 0 时整条不写"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await apply_delta(KEY_A, delta=10, reason="旧事", source=AffectionEventSource.LLM, state_text="旧描述")
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "在吗", timestamp=now_ts() - 120)

        install_upstream(monkeypatch, _FakeUpstream(plain_verdict("", delta=2, reason="新事"), blocks=False))
        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]
        snapshot = await load_snapshot(KEY_A, ttl_seconds=0)
        assert snapshot.state_text == "旧描述"
        assert snapshot.last_event_text == "新事"

        events_before = len(await list_events(KEY_A))
        install_upstream(monkeypatch, _FakeUpstream(plain_verdict("", delta=0, reason="无事"), blocks=False))
        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]
        assert len(await list_events(KEY_A)) == events_before
        assert (await load_snapshot(KEY_A, ttl_seconds=0)).state_text == "旧描述"


@pytest.mark.usefixtures("affection_db")
class TestDeletion:
    """删除：后台「重置」（分数归零 + 清两段描述）当场生效

    只清印象、编辑印象、删流水理由这三项后台操作见 `tests/test_affection_admin_narrative_ops.py`。
    """

    async def test_admin_reset_takes_effect_on_the_very_next_prompt(self) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now_ts() - 60)
        assert POISON_MARK in await render(channel)

        snapshot = await set_score(
            KEY_A,
            score=0,
            reason="管理员重置",
            source=AffectionEventSource.RESET,
            operator="admin",
            clear_state_text=True,
        )

        assert snapshot.score == 0  # 代价：分数一起归零
        assert POISON_MARK not in await render(channel)

    async def test_event_reasons_survive_a_reset_but_never_reach_the_prompt(self) -> None:
        """重置不连带流水（流水理由要删走单独的软删入口），而主对话提示词只读两段描述、不读流水"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await set_score(
            KEY_A,
            score=0,
            reason="管理员重置",
            source=AffectionEventSource.RESET,
            operator="admin",
            clear_state_text=True,
        )
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now_ts() - 60)

        assert any(POISON_MARK in event.reason for event in await list_events(KEY_A))
        assert POISON_MARK not in await render(channel)


@pytest.mark.usefixtures("affection_db")
class TestBlastRadius:
    """记忆域：一份描述跟着「人」走遍同一域、同一人设下的所有频道；换域、换人设就不在"""

    async def test_poison_follows_the_person_within_one_domain_and_preset(self) -> None:
        await poison_the_narrative()  # 写在实例 A、无人设档那一份上
        now = now_ts()
        private_a = _StubChannel(PRIVATE_CHAT_KEY_A, INSTANCE_A, "private")
        group_b = _StubChannel(CHAT_KEY_B, INSTANCE_B)
        other_preset_key = f"onebot_v11-{INSTANCE_A}-group_888"
        other_preset_a = _StubChannel(other_preset_key, INSTANCE_A, preset_id=PRESET_OTHER)
        await add_message(PRIVATE_CHAT_KEY_A, INSTANCE_A, USER_ID, "私聊里说一句", timestamp=now - 30)
        await add_message(CHAT_KEY_B, INSTANCE_B, USER_ID, "另一个实例里说一句", timestamp=now - 20)
        await add_message(other_preset_key, INSTANCE_A, USER_ID, "另一个人设的群里说一句", timestamp=now - 10)

        # 同一实例（自成一域）、同一人设：私聊里照样注入——撞甲一次，这个人到哪儿都带着
        assert POISON_MARK in await render(private_a)
        # 另一个域、另一个人设：互不可见
        assert POISON_MARK not in await render(group_b)
        assert POISON_MARK not in await render(other_preset_a)
        assert await DBAffection.filter(platform_userid=USER_ID).count() == 1
        assert zero_config_key(INSTANCE_B) != KEY_A


@pytest.mark.usefixtures("affection_db")
class TestChannelResetLeavesAffectionAlone:
    """`/reset` 只重置对话上下文：不清好感、不改分数，也没有「顺带清好感」的开关

    群管理就能发 `/reset`，而好感在同域同人设下跨频道共用；一次误触不能清掉这个人在别的群、
    私聊里的印象。修复只把好感**取对话**的下界对齐到 `conversation_start_time`：
    重置之后分数与描述原样留着，只是不再读重置前的对话；人在重置后再开口，原来的描述照常注入。
    """

    async def test_score_and_narrative_survive_a_channel_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = now_ts()
        await apply_delta(KEY_A, delta=40, reason="旧事", source=AffectionEventSource.LLM, state_text="老朋友了。")
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "重置前说的话", timestamp=now - 600)
        channel = _ResettableChannel(CHAT_KEY_A, INSTANCE_A, conversation_start_time=db_now() - timedelta(minutes=5))
        upstream = install_upstream(monkeypatch, _FakeUpstream(plain_verdict("不该被写进去"), blocks=False))

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]
        rendered_before_speaking = await render(channel)

        snapshot = await load_snapshot(KEY_A, ttl_seconds=0)
        assert (snapshot.score, snapshot.state_text) == (40, "老朋友了。")
        assert upstream.prompts == []  # 重置后还没人说话：这一轮什么都不读、什么都不写
        assert "老朋友了" not in rendered_before_speaking

        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "重置后又来了", timestamp=now - 30)
        assert "老朋友了" in await render(channel)

    def test_channel_reset_code_paths_never_touch_affection(self) -> None:
        """`/reset` 的两条入口（Bot 命令、后台按钮）都只走 `DBChatChannel.reset_channel`，它不碰好感"""
        import inspect

        from nekro_agent.models.db_chat_channel import DBChatChannel
        from nekro_agent.services.command.built_in import chat as chat_commands

        assert "affection" not in inspect.getsource(DBChatChannel.reset_channel).lower()
        assert "affection" not in inspect.getsource(chat_commands).lower()


# ---------------------------------------------------------------------------
# 曾经的缺口（已修）
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("affection_db")
class TestBlockedEvaluationLocksTheNarrative:
    """撞甲自锁：评估提示词带着当前描述（`current:`），描述里有撞甲内容 → 评估被上游拦下 →
    这一轮什么都不写 → 描述原样留着、下一轮主对话照注入、下一次评估照样被拦。

    修法：被拦时去掉 `current:` 重试一次，重试成功就照常落库（新描述盖掉旧的）。"""

    async def test_blocked_evaluation_stops_injecting_the_poisoned_narrative(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "今天天气不错", timestamp=now_ts() - 60)
        upstream = install_upstream(monkeypatch, _FakeUpstream(plain_verdict("聊天气，挺平常的。"), blocks=True))

        for _ in range(3):
            evaluator_module.reset_evaluation_throttle()
            await _run_evaluation(channel, make_config())  # type: ignore[arg-type]

        assert upstream.prompts, "评估确实跑了"
        assert POISON_MARK not in await render(channel)

    async def test_harmless_conversation_can_overwrite_a_poisoned_narrative(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        now = now_ts()
        for offset, text in ((90, "今天天气不错"), (60, "晚饭吃了面"), (30, "明天见")):
            await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, text, timestamp=now - offset)
        install_upstream(monkeypatch, _FakeUpstream(plain_verdict("聊天气，挺平常的。"), blocks=True))

        evaluator_module.reset_evaluation_throttle()
        await _run_evaluation(channel, make_config())  # type: ignore[arg-type]

        assert (await load_snapshot(KEY_A, ttl_seconds=0)).state_text == "聊天气，挺平常的。"


@pytest.mark.usefixtures("affection_db")
class TestChannelResetDoesNotReachAffection:
    """`/reset` 只改 `conversation_start_time`；主对话历史按它截断（`templates/history.py`）。

    修法：好感的评估与注入取消息的下界改成 `max(时间窗起点, conversation_start_time)`，
    与主对话历史同一个判据。`/reset` 照旧不清好感、不改分数（见 `TestChannelResetLeavesAffectionAlone`）。"""

    async def test_evaluator_ignores_messages_before_channel_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = now_ts()
        channel = _ResettableChannel(CHAT_KEY_A, INSTANCE_A, conversation_start_time=db_now() - timedelta(minutes=5))
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, f"说点{POISON_MARK}", timestamp=now - 600)
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "重置后正常聊天", timestamp=now - 60)
        upstream = install_upstream(monkeypatch, _FakeUpstream(plain_verdict("重新开始聊。"), blocks=True))

        await _run_evaluation(channel, make_config())  # type: ignore[arg-type]

        assert upstream.prompts, "评估确实跑了"
        assert all(POISON_MARK not in prompt for prompt in upstream.prompts)

    async def test_prompt_skips_people_who_only_spoke_before_channel_reset(self) -> None:
        now = now_ts()
        channel = _ResettableChannel(CHAT_KEY_A, INSTANCE_A, conversation_start_time=db_now() - timedelta(minutes=5))
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "重置前说的话", timestamp=now - 600)
        await add_message(CHAT_KEY_A, INSTANCE_A, OTHER_USER, "重置后别人说的话", timestamp=now - 60)

        assert POISON_MARK not in await render(channel)


@pytest.mark.usefixtures("affection_db")
class TestAdminResetDoesNotStick:
    """后台「重置」本身生效（见 `TestDeletion`），但曾经留不住

    修法：评估只读最近一次后台写入事件之后的对话；落库前在行锁下复核评估开始之后有没有新的后台写入。"""

    async def test_next_evaluation_after_admin_reset_does_not_rewrite_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, f"说点{POISON_MARK}", timestamp=now_ts() - 600)
        await set_score(
            KEY_A,
            score=0,
            reason="管理员重置",
            source=AffectionEventSource.RESET,
            operator="admin",
            clear_state_text=True,
        )

        # 评估模型组没有审核（或审核比主模型松）时，它会照着对话把内容复述进新描述
        def echo(prompt: str) -> Dict[str, Any]:
            feeling = f"还记得他说的{POISON_MARK}。" if POISON_MARK in prompt else "重新认识一下。"
            return {"users": [{"user_id": USER_ID, "delta": 1, "reason": "聊了几句", "feeling": feeling}]}

        install_upstream(monkeypatch, _FakeUpstream(echo, blocks=False))
        await _run_evaluation(channel, make_config())  # type: ignore[arg-type]

        assert POISON_MARK not in (await load_snapshot(KEY_A, ttl_seconds=0)).state_text

    async def test_reset_during_an_inflight_evaluation_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "今天天气不错", timestamp=now_ts() - 60)
        upstream = _FakeUpstream(plain_verdict(f"复述旧印象：{POISON_MARK}"), blocks=False)

        async def admin_resets_meanwhile() -> None:
            await set_score(
                KEY_A,
                score=0,
                reason="管理员重置",
                source=AffectionEventSource.RESET,
                operator="admin",
                clear_state_text=True,
            )

        upstream.before_reply = admin_resets_meanwhile
        install_upstream(monkeypatch, upstream)
        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        assert (await load_snapshot(KEY_A, ttl_seconds=0)).state_text == ""
