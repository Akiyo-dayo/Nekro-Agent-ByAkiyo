"""好感描述「撞甲后清不掉、/reset 无效、后台写入留不住」的修法，逐个分支钉住

`tests/test_affection_narrative_clearing.py` 里那 6 条原先的 strict xfail 是这次修复的验收用例，
它们只看「最后结果对不对」。这一份钉的是**每条修法自己的分支**：

- 评估被内容审核拦下：去掉 `current:` 重试——成功并给出新描述 / 成功但没给新描述（清旧描述、
  分数不动、记 `blocked_clear`）/ 仍被拦（什么都不清）；
- 主对话被拦或被拒答：这个频道抑制期内只注入昵称与阶段，正常回复成功后恢复；
- 评估只读最近一次后台写入之后的对话；进行中的评估遇上后台写入作废（按人）。

撞甲内容用占位串，上游审核用看见它就抛 `LLMContentBlockedError` 的假模型（同上一份文件）。
"""

import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.services.affection.evaluator import EVALUATION_SYSTEM_PROMPT, evaluate_channel
from nekro_agent.services.affection.stage import AffectionEventSource, scale_gain
from nekro_agent.services.affection.store import (
    REASON_BLOCKED_CLEAR,
    REASON_NARRATIVE_EDIT,
    apply_delta,
    clear_narrative,
    edit_narrative,
    list_events,
    load_snapshot,
    set_score,
)
from nekro_agent.services.affection.suppression import (
    is_narrative_suppressed,
    lift_narrative_suppression,
    reset_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.model_capability import LLMFailureKind
from tests.test_affection_narrative_clearing import (
    OTHER_USER,
    POISON_MARK,
    PRIVATE_CHAT_KEY_A,
    _FakeUpstream,
    install_upstream,
    now_ts,
    poison_the_narrative,
    render,
)
from tests.test_affection_system import (
    CHAT_KEY_A,
    INSTANCE_A,
    KEY_A,
    USER_ID,
    _StubChannel,
    add_message,
    affection_db,  # noqa: F401  # pytest 固件靠导入生效
    make_config,
    zero_config_key,
)

OTHER_KEY = zero_config_key(INSTANCE_A, OTHER_USER)


@pytest.fixture(autouse=True)
def _clean_suppression():
    reset_narrative_suppression()
    yield
    reset_narrative_suppression()


def verdicts(*items: Dict[str, Any]) -> Any:
    return lambda _prompt: {"users": list(items)}


def verdict(user_id: str, *, feeling: str, delta: int = 1, reason: str = "聊了几句") -> Dict[str, Any]:
    return {"user_id": user_id, "delta": delta, "reason": reason, "feeling": feeling}


async def sources_of(key: Any) -> List[str]:
    return [event.source for event in reversed(await list_events(key))]


# ---------------------------------------------------------------------------
# 评估被内容审核拦下
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("affection_db")
class TestBlockedEvaluationRetry:
    async def test_retry_without_current_then_new_feeling_lands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "今天天气不错", timestamp=now_ts() - 60)
        upstream = install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="挺平常的。")), blocks=True))

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        assert len(upstream.prompts) == 2
        assert "current:" in upstream.prompts[0] and POISON_MARK in upstream.prompts[0]
        assert "current:" not in upstream.prompts[1]
        snapshot = await load_snapshot(KEY_A, ttl_seconds=0)
        assert snapshot.state_text == "挺平常的。"
        # 拿到了新描述的人不用清：没有 blocked_clear，只有那一条正常的评估
        assert await sources_of(KEY_A) == [AffectionEventSource.LLM.value, AffectionEventSource.LLM.value]

    async def test_retry_with_empty_feeling_clears_old_texts_and_keeps_the_score(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "今天天气不错", timestamp=now_ts() - 60)
        before = await load_snapshot(KEY_A, ttl_seconds=0)
        install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="", delta=0)), blocks=True))

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        after = await load_snapshot(KEY_A, ttl_seconds=0)
        assert (after.state_text, after.last_event_text) == ("", "")
        assert (after.score, after.stage) == (before.score, before.stage)
        latest = (await list_events(KEY_A))[0]
        assert latest.source == AffectionEventSource.BLOCKED_CLEAR.value
        assert (latest.delta, latest.score_after) == (0, before.score)
        assert latest.reason == REASON_BLOCKED_CLEAR
        assert POISON_MARK not in await render(channel)

    async def test_empty_feeling_with_a_delta_clears_first_then_lands_the_delta(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "谢谢你帮忙", timestamp=now_ts() - 60)
        install_upstream(
            monkeypatch,
            _FakeUpstream(verdicts(verdict(USER_ID, feeling="", delta=2, reason="他道了谢")), blocks=True),
        )

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        snapshot = await load_snapshot(KEY_A, ttl_seconds=0)
        assert snapshot.state_text == ""
        assert snapshot.last_event_text == "他道了谢"
        assert snapshot.score == 5 + 2
        assert (await sources_of(KEY_A))[-2:] == [AffectionEventSource.BLOCKED_CLEAR.value, AffectionEventSource.LLM.value]

    async def test_participant_left_out_of_the_retry_is_cleared_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模型没提到的人：旧描述同样在被拦的那份提示词里，同样清；拿到新描述的人不清"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await apply_delta(OTHER_KEY, delta=3, reason="旧事", source=AffectionEventSource.LLM, state_text="另一个人的旧印象")
        now = now_ts()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now - 90)
        await add_message(CHAT_KEY_A, INSTANCE_A, OTHER_USER, "早呀", timestamp=now - 60)
        install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(OTHER_USER, feeling="另一个人的新印象")), blocks=True))

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        assert (await load_snapshot(KEY_A, ttl_seconds=0)).state_text == ""
        assert (await list_events(KEY_A))[0].source == AffectionEventSource.BLOCKED_CLEAR.value
        assert (await load_snapshot(OTHER_KEY, ttl_seconds=0)).state_text == "另一个人的新印象"
        assert AffectionEventSource.BLOCKED_CLEAR.value not in await sources_of(OTHER_KEY)

    async def test_still_blocked_after_retry_clears_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对话本身就会被拦：去掉旧描述照样被拦，这时任何描述都不清、什么都不写"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, f"又说{POISON_MARK}", timestamp=now_ts() - 60)
        before = await load_snapshot(KEY_A, ttl_seconds=0)
        events_before = len(await list_events(KEY_A))
        upstream = install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="x")), blocks=True))

        applied = await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        assert applied == 0
        assert len(upstream.prompts) == 2
        assert await load_snapshot(KEY_A, ttl_seconds=0) == before
        assert len(await list_events(KEY_A)) == events_before

    async def test_nothing_to_strip_means_no_identical_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """提示词里本来就没有 `current:`：重试会是逐字相同的请求，不发，按「对话本身被拦」处理"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, f"说{POISON_MARK}", timestamp=now_ts() - 60)
        upstream = install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="x")), blocks=True))

        assert await evaluate_channel(channel, make_config()) == 0  # type: ignore[arg-type]
        assert len(upstream.prompts) == 1
        assert await DBAffection.all().count() == 0

    def test_evaluation_prompt_asks_for_own_words(self) -> None:
        assert "own words" in EVALUATION_SYSTEM_PROMPT
        assert "Never quote" in EVALUATION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 主对话被拦：频道级抑制
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("affection_db")
class TestNarrativeSuppressionAfterABlockedRound:
    async def test_suppressed_channel_gets_stage_only_until_a_normal_reply(self) -> None:
        group = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        private = _StubChannel(PRIVATE_CHAT_KEY_A, INSTANCE_A, "private")
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now_ts() - 60)
        await add_message(PRIVATE_CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now_ts() - 60)

        suppress_narrative(CHAT_KEY_A)
        rendered = await render(group)
        assert "nickname:" in rendered and "closeness:" in rendered
        assert "feeling:" not in rendered and "recently:" not in rendered
        assert POISON_MARK not in rendered
        # 抑制按频道：同一个人在别的频道照常（描述没被删，只是暂时不往这个频道注入）
        assert POISON_MARK in await render(private)

        lift_narrative_suppression(CHAT_KEY_A)
        assert POISON_MARK in await render(group)

    def test_suppression_lasts_one_context_window(self) -> None:
        window = make_config().AI_CHAT_CONTEXT_EXPIRE_SECONDS
        start = time.time()
        suppress_narrative(CHAT_KEY_A, now=start)
        assert is_narrative_suppressed(CHAT_KEY_A, window, now=start + window - 1)
        assert not is_narrative_suppressed(CHAT_KEY_A, window, now=start + window + 1)


class TestRunAgentWiresTheSuppression:
    """一轮以内容审核拦截 / 供应商拒答收尾 → 记标记；正常收尾 → 摘标记；别的失败不动标记"""

    CHAT_KEY = "onebot_v11-ob10001-group_31"

    async def _run(self, monkeypatch: pytest.MonkeyPatch, outcome: BaseException | None) -> None:
        async def fake_round(**_kwargs: Any) -> None:
            if outcome is not None:
                raise outcome

        async def no_db(*_args: Any, **_kwargs: Any) -> Any:
            return None

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        # 回合首尾的频道额度计数要查消息表，这组用例不建库（理由见 `test_agent_round_control._Harness.install`）
        monkeypatch.setattr(run_agent_module, "_channel_message_marker", no_db)
        monkeypatch.setattr(run_agent_module, "_record_round_reply", no_db)
        ctx = SimpleNamespace(db_chat_channel=SimpleNamespace(chat_key=self.CHAT_KEY, instance_key="ob10001"))
        if outcome is None:
            await run_agent_module.run_agent(self.CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
            return
        # 标记只是副作用：原来的异常必须原样抛给 message_service（它靠异常类型挑回执文案）
        with pytest.raises(type(outcome)) as raised:
            await run_agent_module.run_agent(self.CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
        assert raised.value is outcome

    def suppressed(self) -> bool:
        return is_narrative_suppressed(self.CHAT_KEY, 1800)

    @pytest.mark.parametrize(
        "error",
        [
            run_agent_module.AllLLMRequestsFailedError(kinds=[LLMFailureKind.CONTENT_BLOCKED]),
            run_agent_module.AllLLMRequestsFailedError(kinds=[LLMFailureKind.CONTENT_BLOCKED, LLMFailureKind.OTHER]),
            run_agent_module.AgentProviderRefusalError(marker="m"),
        ],
        ids=["all-blocked", "blocked-then-other", "provider-refusal"],
    )
    async def test_blocked_round_marks_and_normal_round_lifts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error: BaseException,
    ) -> None:
        await self._run(monkeypatch, error)
        assert self.suppressed()
        await self._run(monkeypatch, None)
        assert not self.suppressed()

    @pytest.mark.parametrize(
        "error",
        [
            run_agent_module.AllLLMRequestsFailedError(kinds=[LLMFailureKind.EMPTY_RESPONSE, LLMFailureKind.OTHER]),
            run_agent_module.AgentRoundUnfinishedError("x"),
            run_agent_module.AgentNoExecutableCodeError(),
        ],
        ids=["llm-failed-not-blocked", "unfinished", "no-code"],
    )
    async def test_other_failures_leave_the_mark_alone(self, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
        await self._run(monkeypatch, error)
        assert not self.suppressed()
        suppress_narrative(self.CHAT_KEY)
        await self._run(monkeypatch, error)
        assert self.suppressed()


# ---------------------------------------------------------------------------
# 后台写入留得住
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("affection_db")
class TestEvaluationReadsOnlyAfterTheLatestAdminWrite:
    @pytest.mark.parametrize(
        "admin_write",
        ["reset", "narrative_clear", "narrative_edit"],
    )
    async def test_messages_before_the_admin_write_are_not_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_write: str,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        now = now_ts()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "后台动手之前的旧话", timestamp=now - 600)
        await add_message(CHAT_KEY_A, INSTANCE_A, OTHER_USER, "别人在那之前说的话", timestamp=now - 500)
        if admin_write == "reset":
            await set_score(KEY_A, score=0, reason="重置", source=AffectionEventSource.RESET, operator="admin", clear_state_text=True)
        elif admin_write == "narrative_clear":
            await clear_narrative(KEY_A, source=AffectionEventSource.NARRATIVE_CLEAR, reason="清", operator="admin")
        else:
            await edit_narrative(KEY_A, state_text="管理员写的", last_event_text="", operator="admin")
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "后台动手之后的新话", timestamp=now + 5)
        upstream = install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="新的。")), blocks=False))

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        (prompt,) = upstream.prompts
        assert "后台动手之后的新话" in prompt
        assert "后台动手之前的旧话" not in prompt
        # 同一轮里别人也只读那之后的对话；那之后没说过话的人这一轮跳过
        assert "别人在那之前说的话" not in prompt
        assert f"user_id={OTHER_USER}" not in prompt
        assert not await DBAffection.filter(platform_userid=OTHER_USER).exists()

    async def test_no_message_after_the_admin_write_skips_the_round(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, f"说点{POISON_MARK}", timestamp=now_ts() - 600)
        await clear_narrative(KEY_A, source=AffectionEventSource.NARRATIVE_CLEAR, reason="清", operator="admin")
        upstream = install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="x")), blocks=False))

        assert await evaluate_channel(channel, make_config()) == 0  # type: ignore[arg-type]
        assert upstream.prompts == []

    async def test_manual_score_adjustment_is_not_a_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """人工调分不碰描述，旧对话写回不了它改过的东西，所以不算后台写入"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "调分之前的话", timestamp=now_ts() - 600)
        await set_score(KEY_A, score=40, reason="调", source=AffectionEventSource.MANUAL, operator="admin")
        upstream = install_upstream(monkeypatch, _FakeUpstream(verdicts(verdict(USER_ID, feeling="x")), blocks=False))

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]
        assert "调分之前的话" in upstream.prompts[0]


@pytest.mark.usefixtures("affection_db")
class TestInflightEvaluationLosesToAnAdminWrite:
    async def test_admin_edit_during_the_evaluation_voids_that_persons_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await apply_delta(KEY_A, delta=10, reason="旧事", source=AffectionEventSource.LLM, state_text="旧印象")
        await apply_delta(OTHER_KEY, delta=10, reason="旧事", source=AffectionEventSource.LLM, state_text="别人的旧印象")
        now = now_ts()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now - 90)
        await add_message(CHAT_KEY_A, INSTANCE_A, OTHER_USER, "早呀", timestamp=now - 60)
        upstream = _FakeUpstream(
            verdicts(
                verdict(USER_ID, feeling="评估写的新印象", delta=3),
                verdict(OTHER_USER, feeling="别人的新印象", delta=3),
            ),
            blocks=False,
        )

        async def admin_edits_meanwhile() -> None:
            await edit_narrative(KEY_A, state_text="管理员写的", last_event_text="", operator="admin")

        upstream.before_reply = admin_edits_meanwhile
        install_upstream(monkeypatch, upstream)

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        mine = await load_snapshot(KEY_A, ttl_seconds=0)
        assert (mine.state_text, mine.score) == ("管理员写的", 10)
        assert (await list_events(KEY_A))[0].reason == REASON_NARRATIVE_EDIT
        # 作废只针对管理员动过的那一行：别人照常落库（涨幅按 10 分那一档打过递减收益）
        other = await load_snapshot(OTHER_KEY, ttl_seconds=0)
        assert (other.state_text, other.score) == ("别人的新印象", 10 + scale_gain(3, 10))

    async def test_blocked_clear_also_loses_to_an_admin_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """被拦后的清理同样是评估的结果，同样要过落库前复核"""
        channel = _StubChannel(CHAT_KEY_A, INSTANCE_A)
        await poison_the_narrative()
        await add_message(CHAT_KEY_A, INSTANCE_A, USER_ID, "早", timestamp=now_ts() - 60)
        upstream = _FakeUpstream(verdicts(verdict(USER_ID, feeling="", delta=0)), blocks=True)

        async def admin_edits_meanwhile() -> None:
            await edit_narrative(KEY_A, state_text="管理员写的", last_event_text="管理员写的近况", operator="admin")

        upstream.before_reply = admin_edits_meanwhile
        install_upstream(monkeypatch, upstream)

        await evaluate_channel(channel, make_config())  # type: ignore[arg-type]

        snapshot = await load_snapshot(KEY_A, ttl_seconds=0)
        assert (snapshot.state_text, snapshot.last_event_text) == ("管理员写的", "管理员写的近况")
        assert not await DBAffectionEvent.filter(source=AffectionEventSource.BLOCKED_CLEAR.value).exists()
