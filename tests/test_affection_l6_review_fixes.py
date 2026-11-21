"""好感接记忆独立复核后补的几条契约

1. 里程碑判的是**这一次模型写分**：运营动作（人工调分、合并补偿、入域并入）造成的阶段变化
   不在下一次模型写分时被补记成里程碑；写分之后紧跟着一次运营改分，这次判断作废；
   那条流水的理由在判之前已被删掉，起因不再带上。
2. 「涉及的人」写失败时，刚建的段落要被软删，不能留下一段谁也不指向的有效记忆。
3. 「抄原文」比对两边用同一道清洗：原文里夹着零宽字符也判得出来。
4. 彻底删除实例时，这个实例自成一域的好感行与流水一起删掉；组域的、别的实例的不动。

夹具与写分步骤照抄 `tests/test_affection_milestones_flow.py`（直接从那里导入），
不另造一套：同一份判据只能有一处实现，测试里的等价替身迟早会与真实现分叉。
"""

from typing import List

import pytest

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.services.affection import milestones
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.milestones import quotes_original
from nekro_agent.services.affection.stage import AffectionEventSource
from nekro_agent.services.affection.store import apply_delta, apply_model_delta, redact_event_reason, set_score
from nekro_agent.services.console import instance_manager
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from tests.test_affection_milestones_flow import (  # noqa: F401 - 夹具按名字注入
    ADAPTER_KEY,
    CHAT_KEY,
    INSTANCE_KEY,
    NICKNAME,
    USER_ID,
    embed_calls,
    flow_db,
    memory_on,
    milestone_paragraphs,
    model_write,
    prepare,
    shift_last_ref,
)

ZERO_WIDTH = chr(0x200B)


@pytest.mark.usefixtures("flow_db", "memory_on", "embed_calls")
class TestOnlyModelCrossingsBecomeMilestones:
    async def test_an_operator_change_after_the_last_milestone_is_not_backfilled(
        self,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        _, _, first = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="第一次跨门槛")
        assert first is not None
        await shift_last_ref(key)

        # 管理员手工把分调到「亲近」：这一下不是关系里发生的事
        await set_score(key, score=98, reason="管理员手工调分", source=AffectionEventSource.MANUAL)
        _, _, quiet = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=0, reason="只更新了印象")

        assert quiet is None, "人工调出来的阶段变化不能在下一次模型写分时被补记成里程碑"
        assert len((await DBAffection.get(**key.as_columns())).milestone_refs) == 1

        # 之后模型自己把关系往前推了一格：这一次写入本身跨了阶段，照常记，「从」是写之前的阶段
        _, _, crossed = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=2, reason="说了很多心里话")

        assert crossed is not None
        row = await DBAffection.get(**key.as_columns())
        assert (row.milestone_refs[0]["from"], row.milestone_refs[0]["to"]) == ("close", "trusted")

    async def test_a_model_write_followed_by_an_operator_change_is_stale(
        self,
    ) -> None:
        # 分数落库与判里程碑之间隔着几次 await，管理员恰好在这中间重置：
        # 这次判断是照着重置之前的状态做的，作废
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        _, outcome = await apply_model_delta(
            key,
            delta=1,
            reason="跨过门槛",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
            source_platform_userid=USER_ID,
        )
        await set_score(key, score=0, reason="管理员重置", source=AffectionEventSource.RESET)

        written = await milestones.maybe_record(
            key=key,
            scope=scope,
            channel=channel,  # type: ignore[arg-type]
            outcome=outcome,
            source=AffectionEventSource.LLM,
            reason="跨过门槛",
            nickname=NICKNAME,
            persona_name="测试人设",
            originals=(),
            config=cfg,
            source_account=USER_ID,
        )

        assert written is None
        assert await milestone_paragraphs() == []

    async def test_a_reason_deleted_before_the_milestone_is_written_is_left_out(
        self,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        reason = "他愿意把烦心事讲给我听"
        _, outcome = await apply_model_delta(
            key,
            delta=1,
            reason=reason,
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
            source_platform_userid=USER_ID,
        )
        await redact_event_reason(key, outcome.event_id, operator="admin")

        written = await milestones.maybe_record(
            key=key,
            scope=scope,
            channel=channel,  # type: ignore[arg-type]
            outcome=outcome,
            source=AffectionEventSource.LLM,
            reason=reason,
            nickname=NICKNAME,
            persona_name="测试人设",
            originals=(),
            config=cfg,
            source_account=USER_ID,
        )

        assert written is not None
        paragraph = (await milestone_paragraphs())[0]
        assert paragraph.content == paragraph.summary, "理由已经删了，正文只留模板"
        assert reason not in paragraph.content


@pytest.mark.usefixtures("flow_db", "memory_on", "embed_calls")
class TestPartialFailure:
    async def test_a_persons_write_failure_soft_deletes_the_new_paragraph(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        async def _broken(_paragraph_id: int, _account: str) -> List[str]:
            raise RuntimeError("涉及的人写不进去")

        monkeypatch.setattr(milestones, "_write_persons", _broken)

        _, outcome, written = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="跨过门槛")

        assert written is None
        paragraphs = await milestone_paragraphs()
        assert len(paragraphs) == 1
        assert paragraphs[0].is_inactive is True, "没有流水指向它的段落不能留成有效记忆"
        assert (await DBAffection.get(**key.as_columns())).milestone_refs == []
        assert (await DBAffectionEvent.get(id=outcome.event_id)).memory_paragraph_id is None


class TestQuoteCheck:
    def test_zero_width_characters_in_the_original_do_not_hide_a_quote(self) -> None:
        original = ZERO_WIDTH.join("今晚八点楼下便利店见")

        assert quotes_original("他说今晚八点楼下见", [original]) is True

    def test_an_unrelated_reason_is_still_not_a_quote(self) -> None:
        original = ZERO_WIDTH.join("今晚八点楼下便利店见")

        assert quotes_original("他最近对我很耐心", [original]) is False


@pytest.mark.usefixtures("flow_db")
class TestPurgeInstanceDropsItsOwnAffection:
    async def test_self_domain_rows_and_events_go_with_the_instance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 实例标识是可以被重新认领的：留着自成一域那一份，同名的新实例（可能属于别的租户）
        # 会逐列命中、把印象、称呼、近况与流水原样读出来
        monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})

        async def _no_vectors(_instance_key: str) -> None:
            """向量库不在本用例的契约里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", _no_vectors)

        instance = await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_KEY,
            display_name=INSTANCE_KEY,
            status="online",
            provider=ADAPTER_KEY,
            tenant_id=None,
            metadata_json="",
        )
        own = AffectionKey.of_domain(ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_KEY), person_uid=USER_ID, preset_id=PRESET_NONE)
        other = AffectionKey.of_domain(ScopeDomain.of_instance(ADAPTER_KEY, "ob13579"), person_uid=USER_ID, preset_id=PRESET_NONE)
        grouped = AffectionKey.of_domain(ScopeDomain.of_group(7), person_uid=USER_ID, preset_id=PRESET_NONE)
        for key in (own, other, grouped):
            await apply_delta(key, delta=3, reason="聊得来", source=AffectionEventSource.LLM, state_text="说话很直接")
        await set_score(own, score=10, reason="管理员调分", source=AffectionEventSource.MANUAL)

        counts = await instance_manager.purge_instance(instance)

        assert counts["affection"] == 1
        assert counts["affection_event"] == 2
        assert not await DBAffection.filter(**own.as_columns()).exists()
        assert not await DBAffectionEvent.filter(**own.as_columns()).exists()
        assert await DBAffection.filter(**other.as_columns()).exists(), "别的实例的好感不动"
        assert await DBAffection.filter(**grouped.as_columns()).exists(), "组域上的好感挂在组上，不属于这个实例"
