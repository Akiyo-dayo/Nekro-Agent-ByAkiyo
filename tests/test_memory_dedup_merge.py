"""近重复的软合并：不丢信息、只在人对得上时合并、后台能放回

验收里 0.92 的阈值一对都没挡住，人工看出的几对都是「同一件事隔一会儿又记一遍」或「一条笼统、
一条具体」。合并的口径：
- 只比同一频道、同一人设、近 14 天、「涉及的人」整组相同的沉淀段落；
- 两段都写库，搁置的那段转进隔离区，原因与动作都是 dedup，后台隔离区里看得到、能放回；
- 取舍：受保护或冻结的已有段落一律留下；一边是稳定类型（偏好 / 事实 / 决定）另一边不是，留稳定的；
  其余留较具体的（去掉「Bot 自己说过」前缀后内容较长；一样长时留已有的）；
- 敏感标记两段取「或」，留下的那段记一次强化。
"""

from __future__ import annotations

import json
import math
from datetime import timedelta
from typing import Any

import pytest
from tortoise.timezone import now as db_now

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_paragraph import DBMemParagraph, OriginKind
from nekro_agent.services.memory import admin_service
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator
from tests.test_memory_consolidation_quality import (  # noqa: F401 - 夹具按名字注入
    EMBED_DIMENSION,
    XIAOMAN,
    YOUTH,
    _channel,
    _install_persona,
    _item,
    _message,
    _run,
)
from tests.test_memory_consolidation_quality import memory_env as _quality_env

quality_env = _quality_env

SHORT = "小满抽中了限定角色"
LONG = "小满昨晚十连抽中了限定角色，开心了一晚上"


@pytest.fixture
def same_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有文本同一个向量：相似度恒为 1，判据只剩范围、人与长短"""

    async def embed(_text: str, **_kwargs: object) -> list[float]:
        return [1.0] + [0.0] * (EMBED_DIMENSION - 1)

    monkeypatch.setattr(consolidator_module, "embed_text", embed)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.85)


async def _setup(chat_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    await _channel(chat_key)
    _install_persona(monkeypatch)
    await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="十连出金了")
    await _message(chat_key, index=2, account=YOUTH, nickname="阿青", content="我也出了")


async def _active() -> list[DBMemParagraph]:
    return list(await DBMemParagraph.filter(is_inactive=False, origin_kind=OriginKind.CONSOLIDATION).order_by("id"))


@pytest.mark.usefixtures("quality_env", "same_vector")
class TestSoftMerge:
    async def test_different_people_are_not_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d1"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))
        await _run(chat_key, monkeypatch, _item("阿青抽中了限定角色", persons=["P2"]))

        assert len(await _active()) == 2, "两个人各自抽中是两件事"

    async def test_longer_one_wins_and_the_other_can_be_restored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d2"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"]))

        [kept] = await _active()
        assert kept.content == LONG
        set_aside = await DBMemParagraph.get(content=SHORT)
        assert set_aside.is_inactive is True
        assert admin_service.paragraph_status(set_aside) == "quarantined", "后台隔离区里看得到"
        assert (set_aside.quarantine_reason, set_aside.last_manual_action) == ("dedup", "dedup")

        await admin_service.apply_action("paragraph", set_aside, "restore")
        restored = await DBMemParagraph.get(id=set_aside.id)
        assert restored.is_inactive is False, "能放回"

    async def test_shorter_newcomer_is_set_aside_and_the_old_one_reinforced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_d3"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"]))
        before = await DBMemParagraph.get(content=LONG)
        result = await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))

        assert result.paragraphs_created == 0
        [kept] = await _active()
        assert kept.id == before.id
        assert kept.last_reinforced_at is not None, "留下的那段记一次强化"
        newcomer = await DBMemParagraph.get(content=SHORT)
        assert newcomer.quarantine_reason == "dedup"

    async def test_frozen_existing_is_always_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d4"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))
        await DBMemParagraph.filter(content=SHORT).update(is_frozen=True)
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"]))

        [kept] = await _active()
        assert kept.content == SHORT, "管理员冻结过的段落不被替换"

    async def test_old_paragraphs_are_out_of_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d5"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))
        await DBMemParagraph.filter(content=SHORT).update(create_time=db_now() - timedelta(days=15))
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"]))

        assert len(await _active()) == 2

    async def test_sensitive_flag_is_inherited_both_ways(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d6"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"], sensitive=True))
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"]))
        [kept] = await _active()
        assert kept.content == LONG and kept.is_sensitive is True, "新的替换了敏感的旧段落，标记跟着走"

        other = "onebot_v11-quality-group_d7"
        await _setup(other, monkeypatch)
        await _run(other, monkeypatch, _item(LONG + "。", persons=["P1"]))
        await _run(other, monkeypatch, _item(SHORT, persons=["P1"], sensitive=True))
        kept_other = await DBMemParagraph.get(content=LONG + "。")
        assert kept_other.is_inactive is False and kept_other.is_sensitive is True, "敏感的新来者被并入时，旧段落补上标记"

    async def test_set_aside_copy_carries_the_flag_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d9"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"], sensitive=True))
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))
        newcomer = await DBMemParagraph.get(content=SHORT)
        assert newcomer.is_inactive is True and newcomer.is_sensitive is True, "被搁置的那段放回来时也不能跨频道"

        other = "onebot_v11-quality-group_d10"
        await _setup(other, monkeypatch)
        await _run(other, monkeypatch, _item(SHORT + "。", persons=["P1"]))
        await _run(other, monkeypatch, _item(LONG + "。", persons=["P1"], sensitive=True))
        old = await DBMemParagraph.get(content=SHORT + "。")
        assert old.is_inactive is True and old.is_sensitive is True

    async def test_stable_type_wins_over_longer_chatter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d11"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item("小满不吃香菜", persons=["P1"], knowledge_type="preference"))
        await _run(
            chat_key,
            monkeypatch,
            _item("小满中午点外卖时又说了一遍不要香菜，大家笑他", persons=["P1"], knowledge_type="conversation"),
        )
        [kept] = await _active()
        assert kept.content == "小满不吃香菜", "偏好留下；更长的闲聊按情景半衰期几天就没了"

        other = "onebot_v11-quality-group_d12"
        await _setup(other, monkeypatch)
        await _run(other, monkeypatch, _item("阿青说他晚上一般十一点睡，聊了好一阵作息", persons=["P2"], knowledge_type="conversation"))
        await _run(other, monkeypatch, _item("阿青一般十一点睡", persons=["P2"], knowledge_type="fact"))
        kept_other = [row for row in await _active() if row.origin_chat_key == other]
        assert [row.content for row in kept_other] == ["阿青一般十一点睡"], "后来的稳定类型替换已有的闲聊"

    async def test_bot_said_prefix_is_not_more_specific(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d13"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item("小满抽中了限定角色呢", persons=["P1"]))
        await DBMemParagraph.filter(content="小满抽中了限定角色呢").update(
            content=consolidator_module.BOT_SAID_PREFIX + "小满抽中了限定角色呢",
        )
        await _run(chat_key, monkeypatch, _item("小满抽中了限定角色，很开心", persons=["P1"]))
        [kept] = await _active()
        assert kept.content == "小满抽中了限定角色，很开心", "比长短不算前缀"

    async def test_affection_milestones_are_not_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_d8"
        await _setup(chat_key, monkeypatch)
        await _run(chat_key, monkeypatch, _item(LONG, persons=["P1"]))
        await DBMemParagraph.filter(content=LONG).update(origin_kind=OriginKind.AFFECTION)
        await _run(chat_key, monkeypatch, _item(SHORT, persons=["P1"]))

        milestone = await DBMemParagraph.get(content=LONG)
        assert milestone.is_inactive is False
        assert (await DBMemParagraph.get(content=SHORT)).is_inactive is False


# 灰区判定用的文本：旧段与各条新段之间的余弦相似度分别是 1.00 / 0.88 / 0.70；
# 六条批次文本彼此之间只有 0.88² = 0.7744，低于灰区下限，不会互相顶替。
_OLD_TEXT = "小满抽中了限定角色"
_GREY_TEXT = "小满昨晚十连抽中了限定角色，开心了一晚上"
_LOW_TEXT = "小满今天去食堂吃了面"
_BATCH_TEXTS = [f"小满第{index}次抽中限定角色" for index in range(1, 7)]


def _basis(index: int, first: float) -> list[float]:
    vector = [0.0] * EMBED_DIMENSION
    vector[0] = first
    if index > 0:
        vector[index] = math.sqrt(max(0.0, 1.0 - first * first))
    return vector


def _graded_mapping() -> dict[str, list[float]]:
    mapping = {
        _OLD_TEXT: _basis(0, 1.0),
        _GREY_TEXT: _basis(7, 0.88),
        _LOW_TEXT: _basis(1, 0.70),
    }
    for index, text in enumerate(_BATCH_TEXTS, start=1):
        mapping[text] = _basis(index, 0.88)
    return mapping


@pytest.fixture
def graded_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """按文本给出指定相似度的向量，阈值设 0.92，灰区落在 0.84 ~ 0.92"""
    mapping = _graded_mapping()

    async def embed(text: str, **_kwargs: object) -> list[float]:
        return mapping[text]

    monkeypatch.setattr(consolidator_module, "embed_text", embed)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.92)


def _install_judge_llm(
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict[str, Any]],
    answer: str | Exception,
) -> list[str]:
    prompts: list[str] = []

    async def fake_call(_self: EpisodicConsolidator, prompt: str, *, use_fallback_model: bool = False) -> str:
        del use_fallback_model
        prompts.append(prompt)
        if "<memory_a>" in prompt:
            if isinstance(answer, Exception):
                raise answer
            return answer
        return json.dumps({"memories": items}, ensure_ascii=False)

    monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)
    return prompts


async def _run_judged(
    chat_key: str,
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict[str, Any]],
    answer: str | Exception,
) -> list[str]:
    prompts = _install_judge_llm(monkeypatch, items, answer)
    await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)
    return prompts


def _judge_prompts(prompts: list[str]) -> list[str]:
    return [prompt for prompt in prompts if "<memory_a>" in prompt]


async def _seed_old(chat_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    await _setup(chat_key, monkeypatch)
    await _run(chat_key, monkeypatch, _item(_OLD_TEXT, persons=["P1"]))


@pytest.mark.usefixtures("quality_env", "graded_vectors")
class TestGreyZoneJudge:
    async def test_same_matter_goes_through_soft_merge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_j1"
        await _seed_old(chat_key, monkeypatch)

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_GREY_TEXT, persons=["P1"])], '{"same": true}')

        assert len(_judge_prompts(prompts)) == 1
        [kept] = await _active()
        assert kept.content == _GREY_TEXT, "模型判为同一件事，走现有软合并"
        set_aside = await DBMemParagraph.get(content=_OLD_TEXT)
        assert set_aside.is_inactive is True
        assert set_aside.quarantine_reason == "dedup"

    async def test_not_same_keeps_both_with_vectors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_j2"
        await _seed_old(chat_key, monkeypatch)

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_GREY_TEXT, persons=["P1"])], '{"same": false}')

        assert len(_judge_prompts(prompts)) == 1
        rows = await _active()
        assert {row.content for row in rows} == {_OLD_TEXT, _GREY_TEXT}
        for row in rows:
            assert row.embedding_ref == str(row.id), "两段都正常写入并带上向量"

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param("这不是 JSON", id="garbage"),
            pytest.param('{"same": "true"}', id="string-not-bool"),
            pytest.param(RuntimeError("judge failed"), id="exception"),
        ],
    )
    async def test_bad_answer_counts_as_not_same(
        self,
        monkeypatch: pytest.MonkeyPatch,
        answer: str | Exception,
    ) -> None:
        chat_key = "onebot_v11-quality-group_j3"
        await _seed_old(chat_key, monkeypatch)

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_GREY_TEXT, persons=["P1"])], answer)

        assert len(_judge_prompts(prompts)) == 1
        rows = await _active()
        assert {row.content for row in rows} == {_OLD_TEXT, _GREY_TEXT}, "解析失败或异常都按不重复处理"

    async def test_above_threshold_never_calls_the_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def same_embed(_text: str, **_kwargs: object) -> list[float]:
            return _basis(0, 1.0)

        monkeypatch.setattr(consolidator_module, "embed_text", same_embed)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.85)
        chat_key = "onebot_v11-quality-group_j4"
        await _seed_old(chat_key, monkeypatch)

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_GREY_TEXT, persons=["P1"])], '{"same": true}')

        assert _judge_prompts(prompts) == [], "相似度达到阈值时直接算重复，不调模型"
        [kept] = await _active()
        assert kept.content == _GREY_TEXT

    async def test_below_floor_never_calls_the_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_j5"
        await _seed_old(chat_key, monkeypatch)

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_LOW_TEXT, persons=["P1"])], '{"same": true}')

        assert _judge_prompts(prompts) == [], "低于 0.84 时不算候选，不调模型"
        rows = await _active()
        assert {row.content for row in rows} == {_OLD_TEXT, _LOW_TEXT}

    async def test_sixth_grey_pair_in_a_batch_is_not_judged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chat_key = "onebot_v11-quality-group_j6"
        await _seed_old(chat_key, monkeypatch)

        items = [_item(text, persons=["P1"]) for text in _BATCH_TEXTS]
        prompts = await _run_judged(chat_key, monkeypatch, items, '{"same": false}')

        assert len(_judge_prompts(prompts)) == 5, "一个批次最多判 5 次，第 6 个灰区对不再调模型"
        rows = await _active()
        assert len(rows) == 7, "超过上限的灰区按不重复，新段落照常留下"

    async def test_zero_threshold_skips_the_whole_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.0)
        chat_key = "onebot_v11-quality-group_j7"
        await _seed_old(chat_key, monkeypatch)

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_GREY_TEXT, persons=["P1"])], '{"same": true}')

        assert _judge_prompts(prompts) == []
        rows = await _active()
        assert {row.content for row in rows} == {_OLD_TEXT, _GREY_TEXT}

    async def test_gate_off_path_never_reads_the_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        chat_key = "onebot_v11-quality-group_j8"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="十连出金了")
        await _run(chat_key, monkeypatch, _item(_OLD_TEXT))
        await _message(chat_key, index=2, account=YOUTH, nickname="阿青", content="我也出了")

        prompts = await _run_judged(chat_key, monkeypatch, [_item(_GREY_TEXT)], '{"same": true}')

        assert _judge_prompts(prompts) == [], "闸关路径逐字不变，不查近重复也不调模型"
        assert await DBMemParagraph.all().count() == 2
