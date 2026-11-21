"""沉淀关系的保留时长跟随来源段落

关系原来一律按 1 天的半衰期衰减、约 5 天就被清，不管它是从哪段记忆里抽出来的：偏好、事实、决定
这类段落能留 30 天，它的关系与只出现过一次的实体却 5 天就没了（关系通道、记忆详情与图谱都受影响）。

1. 沉淀时关系的半衰期不短于来源段落；同一条三元组被稳定类型的段落再次提到时补长、不缩短；
2. 存量里来源段落仍有效且是稳定类型的有效关系，清理之前先补长（只改半衰期、幂等）；
3. 一次性捞回被自动清理掉、来源段落仍有效且是稳定类型的关系，连同实体；管理员删的、手动清理
   清的不碰；同名的有效实体已经新建过的挂到那一行上，同一条三元组已经有有效的就不再恢复。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import maintenance
from nekro_agent.services.memory import relation_retention as relation_retention_module
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory.relation_retention import (
    extend_stable_relation_half_life,
    relation_half_life_seconds,
    relation_restore_done,
    restore_pruned_stable_relations,
)
from nekro_agent.services.memory.retention import MANUAL_PRUNE_ACTION, RETENTION_RESTORE_ACTION
from nekro_agent.services.memory.scheduler import MemoryMaintenanceScheduler
from tests.test_memory_consolidation_quality import (  # noqa: F401 - 夹具按名字注入
    ADAPTER,
    INSTANCE,
    _channel,
    _install_persona,
    _item,
    _message,
    _run,
    memory_env,
)

DAY = 86400
CHAT = "onebot_v11-quality-group_rel"


def _ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _paragraph(
    knowledge_type: KnowledgeType = KnowledgeType.FACT,
    *,
    inactive: bool = False,
    action: str = "",
    action_at: Optional[datetime] = None,
    half_life_days: float = 30,
) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key=INSTANCE,
        adapter_key=ADAPTER,
        origin_chat_key=CHAT,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=knowledge_type,
        content=f"{knowledge_type.value} 段落",
        summary=f"{knowledge_type.value} 段落",
        event_time=_ago(1),
        half_life_seconds=int(half_life_days * DAY),
        origin_kind=OriginKind.CONSOLIDATION,
        is_inactive=inactive,
        last_manual_action=action,
        last_manual_action_at=action_at,
    )


async def _entity(name: str, *, inactive: bool = False) -> DBMemEntity:
    return await DBMemEntity.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=CHAT,
        entity_type=EntityType.CONCEPT,
        name=name,
        canonical_name=name.lower(),
        is_inactive=inactive,
    )


async def _relation(
    paragraph: Optional[DBMemParagraph],
    subject: DBMemEntity,
    obj: DBMemEntity,
    *,
    age_days: float,
    deactivated_at: Optional[datetime] = None,
    predicate: str = "喜欢",
    half_life_days: float = 1,
) -> DBMemRelation:
    row = await DBMemRelation.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=CHAT,
        subject_entity_id=subject.id,
        predicate=predicate,
        object_entity_id=obj.id,
        paragraph_id=paragraph.id if paragraph is not None else None,
        memory_source="na",
        cognitive_type="episodic",
        half_life_seconds=int(half_life_days * DAY),
    )
    await DBMemRelation.filter(id=row.id).update(create_time=_ago(age_days))
    if deactivated_at is not None:
        await DBMemRelation.filter(id=row.id).update(is_inactive=True, update_time=deactivated_at)
    return await DBMemRelation.get(id=row.id)


async def _pair(prefix: str, *, inactive: bool = False) -> tuple[DBMemEntity, DBMemEntity]:
    return await _entity(f"{prefix}甲", inactive=inactive), await _entity(f"{prefix}乙", inactive=inactive)


class TestHalfLifeRule:
    def test_follows_the_source_paragraph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_EPISODIC_HALF_LIFE_SECONDS", DAY)
        monkeypatch.setattr(config, "MEMORY_RELATION_HALF_LIFE_SECONDS", DAY)
        for kind in (KnowledgeType.PREFERENCE, KnowledgeType.FACT, KnowledgeType.DECISION):
            assert relation_half_life_seconds(kind) == 30 * DAY
        assert relation_half_life_seconds(KnowledgeType.CONVERSATION) == DAY

    def test_never_shorter_than_the_relation_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_EPISODIC_HALF_LIFE_SECONDS", DAY)
        monkeypatch.setattr(config, "MEMORY_RELATION_HALF_LIFE_SECONDS", 3 * DAY)
        assert relation_half_life_seconds(KnowledgeType.EXPERIENCE) == 3 * DAY


@pytest.mark.usefixtures("memory_env")
class TestConsolidationWritesIt:
    async def _relation_after(self, monkeypatch: pytest.MonkeyPatch, chat_key: str, **item: Any) -> DBMemRelation:
        await _channel(chat_key)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account="1234567", nickname="小满", content="我不吃香菜")
        await _run(
            chat_key,
            monkeypatch,
            _item(
                item.pop("content", "小满不吃香菜"),
                entities=["小满", "香菜"],
                relations=[{"subject": "小满", "predicate": "不吃", "object": "香菜"}],
                **item,
            ),
        )
        return await DBMemRelation.filter(origin_chat_key=chat_key).order_by("id").first()

    async def test_stable_paragraph_gives_its_relation_the_semantic_half_life(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        relation = await self._relation_after(monkeypatch, "onebot_v11-quality-group_r1", knowledge_type="preference")
        assert relation.half_life_seconds == 30 * DAY

    async def test_other_types_keep_the_short_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_RELATION_HALF_LIFE_SECONDS", DAY)
        monkeypatch.setattr(config, "MEMORY_EPISODIC_HALF_LIFE_SECONDS", DAY)
        relation = await self._relation_after(monkeypatch, "onebot_v11-quality-group_r2", knowledge_type="conversation")
        assert relation.half_life_seconds == DAY

    async def test_existing_triple_is_extended_when_a_stable_paragraph_repeats_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0)
        chat_key = "onebot_v11-quality-group_r3"
        first = await self._relation_after(monkeypatch, chat_key, knowledge_type="conversation", content="小满说过不吃香菜")
        assert first.half_life_seconds == DAY
        await _message(chat_key, index=2, account="1234567", nickname="小满", content="香菜真的不行")
        await _run(
            chat_key,
            monkeypatch,
            _item(
                "小满不吃香菜",
                knowledge_type="fact",
                entities=["小满", "香菜"],
                relations=[{"subject": "小满", "predicate": "不吃", "object": "香菜"}],
            ),
        )
        rows = await DBMemRelation.filter(origin_chat_key=chat_key, is_inactive=False)
        assert [row.half_life_seconds for row in rows] == [30 * DAY], "同一条三元组复用、补长，不另建"


@pytest.mark.usefixtures("memory_env")
class TestStockExtension:
    async def test_only_active_relations_of_active_stable_paragraphs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        stable = await _paragraph(KnowledgeType.PREFERENCE)
        chatter = await _paragraph(KnowledgeType.CONVERSATION, half_life_days=1)
        gone = await _paragraph(KnowledgeType.FACT, inactive=True, action="prune", action_at=_ago(1))
        a, b = await _pair("补齐")
        extended = await _relation(stable, a, b, age_days=1)
        untouched = [
            await _relation(chatter, a, b, age_days=1, predicate="聊过"),
            await _relation(gone, a, b, age_days=1, predicate="说过"),
            await _relation(stable, a, b, age_days=1, predicate="删过", deactivated_at=_ago(0.5)),
            await _relation(None, a, b, age_days=1, predicate="没来源"),
        ]

        assert await extend_stable_relation_half_life() == 1
        assert (await DBMemRelation.get(id=extended.id)).half_life_seconds == 30 * DAY
        for row in untouched:
            assert (await DBMemRelation.get(id=row.id)).half_life_seconds == DAY
        assert await extend_stable_relation_half_life() == 0, "幂等"

    async def test_prune_extends_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_PRUNE_RELATION_THRESHOLD", 0.03)
        stable = await _paragraph(KnowledgeType.FACT)
        chatter = await _paragraph(KnowledgeType.CONVERSATION)  # 段落本身留着，只看关系
        a, b = await _pair("清理")
        kept = await _relation(stable, a, b, age_days=6)  # 按 1 天早在 0.03 以下
        dropped = await _relation(chatter, a, b, age_days=6, predicate="聊过")

        await maintenance.prune_all_workspaces()

        assert (await DBMemRelation.get(id=kept.id)).is_inactive is False, "稳定段落的关系先补长，这一轮不清"
        assert (await DBMemRelation.get(id=dropped.id)).is_inactive is True
        assert (await DBMemEntity.get(id=a.id)).is_inactive is False, "还被有效关系引用的实体不是孤儿"


@pytest.mark.usefixtures("memory_env")
class TestOneOffRestore:
    @pytest.fixture(autouse=True)
    def _thresholds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_PRUNE_RELATION_THRESHOLD", 0.03)

    async def test_pruned_relation_comes_back_with_its_entities(self) -> None:
        stable = await _paragraph(KnowledgeType.FACT)
        lonely = await _entity("孤儿实体", inactive=True)  # 关系被清后它被当成孤儿清掉，没人再提
        renamed = await _entity("又提到了", inactive=True)
        twin = await _entity("又提到了")  # 清理之后同名又被提到，已经新建了一行有效的
        pruned = await _relation(stable, lonely, renamed, age_days=8, deactivated_at=_ago(1))

        result = await restore_pruned_stable_relations()

        fresh = await DBMemRelation.get(id=pruned.id)
        assert (fresh.is_inactive, fresh.half_life_seconds) == (False, 30 * DAY)
        assert fresh.subject_entity_id == lonely.id and (await DBMemEntity.get(id=lonely.id)).is_inactive is False
        assert fresh.object_entity_id == twin.id, "挂到已有的同名有效实体上"
        assert (await DBMemEntity.get(id=renamed.id)).is_inactive is True, "不造出第二个同名的有效实体"
        assert (result.restored, result.entities_reactivated, result.entities_repointed) == (1, 1, 1)
        assert result.finished and relation_restore_done()

    async def test_what_stays_down(self) -> None:
        stable = await _paragraph(KnowledgeType.PREFERENCE)
        chatter = await _paragraph(KnowledgeType.CONVERSATION)
        a, b = await _pair("不捞")
        deleted_live = await _relation(stable, a, b, age_days=1, deactivated_at=_ago(0.5), predicate="管理员删的")
        of_chatter = await _relation(chatter, a, b, age_days=8, deactivated_at=_ago(1), predicate="闲聊的")
        too_old = await _relation(stable, a, b, age_days=200, deactivated_at=_ago(190), predicate="太老")
        manual_at = _ago(2)
        await _paragraph(KnowledgeType.CONVERSATION, inactive=True, action=MANUAL_PRUNE_ACTION, action_at=manual_at)
        by_hand = await _relation(stable, a, b, age_days=8, deactivated_at=manual_at, predicate="手动清理的")

        result = await restore_pruned_stable_relations()

        for row in (deleted_live, of_chatter, too_old, by_hand):
            assert (await DBMemRelation.get(id=row.id)).is_inactive is True
        assert (result.restored, result.not_auto_pruned, result.below_line) == (0, 2, 1)

    async def test_relation_that_went_with_a_restored_paragraph(self) -> None:
        restored_at = _ago(0.1)
        paragraph = await _paragraph(KnowledgeType.FACT, action=RETENTION_RESTORE_ACTION, action_at=restored_at)
        a, b = await _pair("随段落")
        # 段落被清那一轮，关系是「来源段落被清」带下去的，那一刻按自己的权重还在线上
        went_along = await _relation(paragraph, a, b, age_days=4, deactivated_at=_ago(0.5))

        result = await restore_pruned_stable_relations()

        assert (await DBMemRelation.get(id=went_along.id)).is_inactive is False
        assert result.restored == 1

    async def test_triple_already_active_is_not_duplicated(self) -> None:
        stable = await _paragraph(KnowledgeType.FACT)
        a, b = await _pair("重复")
        pruned = await _relation(stable, a, b, age_days=8, deactivated_at=_ago(1))
        await _relation(stable, a, b, age_days=0.1)  # 同一条三元组后来又建了一条有效的

        result = await restore_pruned_stable_relations()

        assert (await DBMemRelation.get(id=pruned.id)).is_inactive is True
        assert (result.restored, result.already_present) == (0, 1)

    async def test_one_off_and_limit(self) -> None:
        stable = await _paragraph(KnowledgeType.DECISION)
        a, b = await _pair("名额")
        for index in range(3):
            # 被自动清理的（失活时已在线下），可按语义半衰期算现在也还在线下：翻得到，但不占名额
            await _relation(stable, a, b, age_days=400, deactivated_at=_ago(390), predicate=f"线下{index}")
        first = await _relation(stable, a, b, age_days=8, deactivated_at=_ago(1), predicate="一")
        second = await _relation(stable, a, b, age_days=8, deactivated_at=_ago(1), predicate="二")

        assert (await restore_pruned_stable_relations(limit=1, page_size=2)).restored == 1
        assert (await DBMemRelation.get(id=first.id)).is_inactive is False, "线下的不占名额"
        assert not relation_restore_done(), "撞上名额不记标记"
        assert (await restore_pruned_stable_relations(limit=1, page_size=2)).restored == 1
        assert (await DBMemRelation.get(id=second.id)).is_inactive is False
        assert relation_restore_done()

        later = await _relation(stable, a, b, age_days=8, deactivated_at=_ago(1), predicate="之后")
        assert (await restore_pruned_stable_relations()).restored == 0, "一次性：做完以后不再捞"
        assert (await DBMemRelation.get(id=later.id)).is_inactive is True

    async def test_failure_leaves_it_for_next_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stable = await _paragraph(KnowledgeType.FACT)
        a, b = await _pair("失败")
        pruned = await _relation(stable, a, b, age_days=8, deactivated_at=_ago(1))
        original = relation_retention_module._revive  # noqa: SLF001

        async def broken(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("库暂时不可用")

        monkeypatch.setattr(relation_retention_module, "_revive", broken)
        result = await restore_pruned_stable_relations()
        assert (result.failed, result.finished) == (1, False)
        assert not relation_restore_done()

        monkeypatch.setattr(relation_retention_module, "_revive", original)
        assert (await restore_pruned_stable_relations()).restored == 1
        assert (await DBMemRelation.get(id=pruned.id)).is_inactive is False

    def test_marker_is_isolated_per_test(self, tmp_path: Any) -> None:
        assert relation_retention_module.RELATION_RESTORE_MARKER_PATH.parent == tmp_path


class TestSchedulerOrder:
    @pytest.mark.asyncio
    async def test_paragraphs_first_then_relations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: List[str] = []

        def recorder(name: str) -> Any:
            async def record(*_args: Any, **_kwargs: Any) -> int:
                calls.append(name)
                return 0

            return record

        for name in (
            "extend_stable_half_life",
            "extend_stable_relation_half_life",
            "restore_pruned_stable",
            "restore_pruned_stable_relations",
        ):
            monkeypatch.setattr(scheduler_module, name, recorder(name))
        await MemoryMaintenanceScheduler()._extend_stable_half_life()  # noqa: SLF001
        assert calls == [
            "extend_stable_half_life",
            "extend_stable_relation_half_life",
            "restore_pruned_stable",
            "restore_pruned_stable_relations",
        ], "段落先捞回，跟着段落被清的关系才在范围里"
