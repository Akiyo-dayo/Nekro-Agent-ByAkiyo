"""好感里程碑纯函数的单元测试

判据（要不要写）、模板（写什么）与清洗（能不能写）这三段规则都在纯函数里，不碰库也不碰
模型，因此可以逐条钉死。这里只测这些纯函数：写库、并发、嵌入属于另外的用例，混进来会让
一条判据的失败原因变得难查。

阶段顺序一律从 `stage.all_stages()` 取，不在用例里抄一份：抄一份就等于把「顺序」这件事
实现了两遍，实现改了顺序而用例没改时，两边一起错还会互相印证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nekro_agent.services.affection.milestones import (
    MILESTONE_NICKNAME_MAX_CHARS,
    MILESTONE_PERSONA_MAX_CHARS,
    MILESTONE_REASON_MAX_CHARS,
    clean_reason,
    decide_milestone,
    milestone_band,
    quotes_original,
    render_milestone_text,
)
from nekro_agent.services.affection.stage import AffectionStage, all_stages, stage_label

# 判据的固定时刻：全部用同一个起点加上明确的偏移，避免用例之间靠「现在」互相牵连。
T0 = datetime(2024, 11, 10, 9, 0, tzinfo=timezone.utc)
COOLDOWN_HOURS = 24


def _ref(stage_from: AffectionStage, stage_to: AffectionStage, at: datetime) -> dict:
    """造一条里程碑引用，字段形状与实现写库时一致"""
    return {
        "paragraph_id": 7,
        "event_id": 11,
        "from": stage_from.value,
        "to": stage_to.value,
        "at": at.isoformat(),
    }


def _decide(
    refs: list[dict],
    stage_before: AffectionStage,
    stage_now: AffectionStage,
    at: datetime,
    cooldown_hours: int = COOLDOWN_HOURS,
):
    return decide_milestone(
        refs=refs,
        stage_before=stage_before,
        stage_now=stage_now,
        now=at,
        cooldown_hours=cooldown_hours,
    )


class TestQuotesOriginal:
    """起因里有没有照抄用户原话"""

    def test_six_char_overlap_is_detected(self) -> None:
        assert quotes_original("他刚才说我们一起去了公园", ["我们一起去了公园"]) is True

    def test_whitespace_does_not_hide_overlap(self) -> None:
        # 模型抄原话时常重新断行、加空格；按原样比对会漏，所以两边都要先去空白
        assert quotes_original("他说「我们 一起去了\n公园」", ["我们一 起去 了公园"]) is True
        assert quotes_original("我们\t一起去了公园", ["我们一起去了公园"]) is True

    def test_short_text_after_squashing_is_not_an_overlap(self) -> None:
        # 去空白后不足 6 字：太短的片段分不清「抄」与「巧合」
        assert quotes_original("我们 一起", ["我们一起去了公园"]) is False
        assert quotes_original("   \n  ", ["我们一起去了公园"]) is False

    def test_five_char_overlap_is_below_the_window(self) -> None:
        # 窗口是 6，5 字重合不该被判成抄：中文里这种常见搭配太多
        assert quotes_original("一起去了公", ["我们一起去了公园"]) is False
        assert quotes_original("一起去了公园", ["我们一起去了公园"]) is True

    def test_no_originals_means_no_quote(self) -> None:
        assert quotes_original("我们一起去了公园散步", []) is False
        assert quotes_original("我们一起去了公园散步", [""]) is False


class TestMilestoneBand:
    """上一条里程碑的回差格"""

    def test_interior_stage_covers_one_step_toward_where_it_came_from(self) -> None:
        order = all_stages()
        for index in range(1, len(order) - 1):
            stage = order[index]
            # 从更低处升上来：让出下面那一格，正好覆盖「刚跨过来的那一步」
            assert milestone_band(stage, order[index - 1]) == frozenset({stage, order[index - 1]})
            # 从更高处掉下来：让出上面那一格
            assert milestone_band(stage, order[index + 1]) == frozenset({stage, order[index + 1]})

    def test_same_stage_covers_only_itself(self) -> None:
        for stage in all_stages():
            assert milestone_band(stage, stage) == frozenset({stage})

    def test_end_stages_have_no_outward_neighbour(self) -> None:
        order = all_stages()
        bottom, top = order[0], order[-1]
        # 最底一阶朝外没有阶段可让，只剩自己与朝里的那一格
        assert milestone_band(bottom, order[1]) == frozenset({bottom, order[1]})
        assert milestone_band(bottom, bottom) == frozenset({bottom})
        # 最顶一阶同理
        assert milestone_band(top, order[-2]) == frozenset({top, order[-2]})
        assert milestone_band(top, top) == frozenset({top})

    def test_band_never_widens_beyond_two_stages(self) -> None:
        order = all_stages()
        for stage_to in order:
            for stage_from in order:
                assert len(milestone_band(stage_to, stage_from)) <= 2


class TestDecideMilestoneSequence:
    """从第一条到和好的完整序列"""

    def test_first_write_without_refs(self) -> None:
        assert _decide([], AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, T0) == (
            AffectionStage.ACQUAINTED,
            AffectionStage.FRIENDLY,
        )

    def test_first_write_with_unchanged_stage_is_not_recorded(self) -> None:
        # 阶段没变就不是「跨过门槛」，哪怕这是这一份好感的第一条
        assert _decide([], AffectionStage.FRIENDLY, AffectionStage.FRIENDLY, T0) is None

    def test_full_sequence(self) -> None:
        # 第一步：没有引用，眼熟写到熟络
        assert _decide([], AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, T0) == (
            AffectionStage.ACQUAINTED,
            AffectionStage.FRIENDLY,
        )
        refs = [_ref(AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, T0)]

        # 门槛附近来回跳：眼熟与熟络都在回差格里，冷却早就过了也不写
        later = T0 + timedelta(hours=COOLDOWN_HOURS * 3)
        assert _decide(refs, AffectionStage.FRIENDLY, AffectionStage.ACQUAINTED, later) is None
        assert _decide(refs, AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, later) is None

        # 真正升到亲近：出了回差格，距上一条已满冷却，从「上一条的到」写起
        assert _decide(refs, AffectionStage.FRIENDLY, AffectionStage.CLOSE, later) == (
            AffectionStage.FRIENDLY,
            AffectionStage.CLOSE,
        )
        refs.insert(0, _ref(AffectionStage.FRIENDLY, AffectionStage.CLOSE, later))

        # 冷却没到时掉到眼熟：这次不写，等冷却过后下一次模型写分补上
        soon = later + timedelta(hours=1)
        assert _decide(refs, AffectionStage.CLOSE, AffectionStage.ACQUAINTED, soon) is None

        # 冷却过后阶段仍是眼熟：补记这一步
        cooled = later + timedelta(hours=COOLDOWN_HOURS)
        assert _decide(refs, AffectionStage.ACQUAINTED, AffectionStage.ACQUAINTED, cooled) == (
            AffectionStage.CLOSE,
            AffectionStage.ACQUAINTED,
        )
        refs.insert(0, _ref(AffectionStage.CLOSE, AffectionStage.ACQUAINTED, cooled))

        # 回到亲近：回差格只覆盖眼熟与熟络，亲近在格子上方，因此要写
        back = cooled + timedelta(hours=COOLDOWN_HOURS)
        assert _decide(refs, AffectionStage.ACQUAINTED, AffectionStage.CLOSE, back) == (
            AffectionStage.ACQUAINTED,
            AffectionStage.CLOSE,
        )

    def test_cooldown_boundary_counts_as_full(self) -> None:
        refs = [_ref(AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, T0)]
        exactly = T0 + timedelta(hours=COOLDOWN_HOURS)
        assert _decide(refs, AffectionStage.FRIENDLY, AffectionStage.CLOSE, exactly) == (
            AffectionStage.FRIENDLY,
            AffectionStage.CLOSE,
        )
        just_before = exactly - timedelta(minutes=1)
        assert _decide(refs, AffectionStage.FRIENDLY, AffectionStage.CLOSE, just_before) is None

    def test_refs_are_read_newest_first(self) -> None:
        # 只看最前面那一条：旧的那条即便早就满了冷却，也不能拿来当判据
        old = _ref(AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, T0 - timedelta(days=30))
        newest = _ref(AffectionStage.FRIENDLY, AffectionStage.CLOSE, T0)
        refs = [newest, old]
        assert _decide(refs, AffectionStage.CLOSE, AffectionStage.ACQUAINTED, T0 + timedelta(hours=1)) is None

    @pytest.mark.parametrize(
        "dirty_ref",
        [
            # from 不是合法阶段
            {"paragraph_id": 7, "event_id": 11, "from": "不存在的阶段", "to": "friendly", "at": T0.isoformat()},
            # at 解析不了
            {"paragraph_id": 7, "event_id": 11, "from": "acquainted", "to": "friendly", "at": "昨天下午"},
            # 缺键
            {"paragraph_id": 7, "event_id": 11, "from": "acquainted", "to": "friendly"},
            # 键在但类型不对
            {"paragraph_id": 7, "event_id": 11, "from": "acquainted", "to": "friendly", "at": None},
        ],
    )
    def test_dirty_first_ref_is_treated_as_no_ref(self, dirty_ref: dict) -> None:
        # 脏值只可能来自手工改库；拿半条数据去比阶段比整条丢掉更危险
        assert _decide([dirty_ref], AffectionStage.ACQUAINTED, AffectionStage.FRIENDLY, T0) == (
            AffectionStage.ACQUAINTED,
            AffectionStage.FRIENDLY,
        )
        # 按「没有上一条」处理，因此阶段没变时照旧不写
        assert _decide([dirty_ref], AffectionStage.ACQUAINTED, AffectionStage.ACQUAINTED, T0) is None

    def test_naive_ref_time_is_read_as_utc(self) -> None:
        # 手工改库可能留下不带时区的值：按 UTC 解释，既不能抛异常，
        # 也不能悄悄按本机时区把它挪到几个小时之前，否则冷却会提前满足
        naive = {"paragraph_id": 7, "event_id": 11, "from": "acquainted", "to": "friendly", "at": "2024-11-10T09:00:00"}
        within_cooldown = datetime(2024, 11, 11, 5, 0, tzinfo=timezone.utc)  # 20 小时后
        assert _decide([naive], AffectionStage.FRIENDLY, AffectionStage.CLOSE, within_cooldown) is None
        past_cooldown = datetime(2024, 11, 11, 10, 0, tzinfo=timezone.utc)  # 25 小时后
        assert _decide([naive], AffectionStage.FRIENDLY, AffectionStage.CLOSE, past_cooldown) == (
            AffectionStage.FRIENDLY,
            AffectionStage.CLOSE,
        )


class TestRenderMilestoneText:
    """正文与摘要"""

    def _render(self, **overrides):
        params = {
            "when": T0,
            "persona_name": "小助手",
            "nickname": "阿岚",
            "stage_from": AffectionStage.ACQUAINTED,
            "stage_to": AffectionStage.CLOSE,
            "reason": "一起把那段代码看完了",
        }
        params.update(overrides)
        return render_milestone_text(**params)

    def test_summary_is_whole_sentence_without_reason(self) -> None:
        content, summary = self._render()
        local = T0.astimezone()
        assert summary == (
            f"{local.month} 月 {local.day} 日，小助手和「阿岚」的关系"
            f"从「{stage_label(AffectionStage.ACQUAINTED)}」变成了「{stage_label(AffectionStage.CLOSE)}」。"
        )
        # 摘要要能独立成句：删理由时直接拿它覆盖正文，不能留着「从 → 到」这种半截话
        assert "起因" not in summary
        assert summary.endswith("。")
        assert content == summary + "起因：一起把那段代码看完了"

    def test_empty_reason_keeps_content_equal_to_summary(self) -> None:
        content, summary = self._render(reason="")
        assert content == summary

    def test_empty_persona_and_nickname_fall_back_to_placeholders(self) -> None:
        _, summary = self._render(persona_name="   ", nickname="")
        assert "Bot和「对方」" in summary

    def test_stage_names_use_labels_not_raw_values(self) -> None:
        _, summary = self._render()
        assert f"「{stage_label(AffectionStage.ACQUAINTED)}」" in summary
        assert f"「{stage_label(AffectionStage.CLOSE)}」" in summary
        assert AffectionStage.ACQUAINTED.value not in summary
        assert AffectionStage.CLOSE.value not in summary

    def test_long_nickname_is_truncated_at_the_limit(self) -> None:
        nickname = "很" * (MILESTONE_NICKNAME_MAX_CHARS + 8)
        _, summary = self._render(nickname=nickname)
        assert f"「{'很' * MILESTONE_NICKNAME_MAX_CHARS}…」" in summary
        # 上限之外的字符不能漏进正文
        assert "很" * (MILESTONE_NICKNAME_MAX_CHARS + 1) not in summary

    def test_long_persona_name_is_truncated_at_the_limit(self) -> None:
        persona = "设" * (MILESTONE_PERSONA_MAX_CHARS + 8)
        _, summary = self._render(persona_name=persona)
        assert f"{'设' * MILESTONE_PERSONA_MAX_CHARS}…和「" in summary
        assert "设" * (MILESTONE_PERSONA_MAX_CHARS + 1) not in summary


class TestCleanReason:
    """起因的清洗"""

    def test_normal_reason_is_returned_cleaned(self) -> None:
        # 换行与连续空格压成单行：起因会拼进一句正文，不能自己造出新的一行
        assert clean_reason("  今天   一起   看了很久的代码\n然后聊了点别的  ", []) == (
            "今天 一起 看了很久的代码 然后聊了点别的"
        )

    def test_copied_original_is_dropped(self) -> None:
        assert clean_reason("他跟我说「我们 一起去了 公园」", ["我们一起去了公园"]) == ""
        assert clean_reason("我们一起去了公园散步", ["我们一起去了公园"]) == ""

    def test_empty_after_cleaning_is_dropped(self) -> None:
        assert clean_reason("   \n\t  ", []) == ""
        assert clean_reason("", []) == ""

    def test_reason_is_truncated_at_the_limit(self) -> None:
        reason = "今" * (MILESTONE_REASON_MAX_CHARS + 20)
        cleaned = clean_reason(reason, [])
        assert cleaned == "今" * MILESTONE_REASON_MAX_CHARS + "…"
        assert len(cleaned) <= MILESTONE_REASON_MAX_CHARS + 1

    def test_reason_exactly_at_the_limit_is_untouched(self) -> None:
        reason = "今" * MILESTONE_REASON_MAX_CHARS
        assert clean_reason(reason, []) == reason

    def test_short_shared_phrase_is_kept(self) -> None:
        # 只有 4 字重合（「一起去了」）是常用搭配，不该当成抄原话把整句丢掉
        assert clean_reason("今天一起去了", ["我们昨天一起去了公园"]) == "今天一起去了"
