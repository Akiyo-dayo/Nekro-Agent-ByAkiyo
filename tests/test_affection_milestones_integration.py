"""好感里程碑在读取、联动与后台接口上的集成契约。

## 这份用例钉什么

里程碑不是一张独立的表：它就是一行「来源 = 好感」的普通记忆段落，另外由
好感行上的 `milestone_refs` 与流水上的 `memory_paragraph_id` 把它和一段关系绑在一起。
因此它的正确性分散在十条互不相干的链路上：提示词注入、自动记忆块回表、频道重建、
自动清理、被拦轮次计数、好感重置、删流水理由、入域折叠、孤儿人设清理、后台详情与删除。
每一条都能单独写错，而写错的地方又都不会让别的用例变红。这里按链路逐条钉住。

## 为什么直接建行，而不是走评估链路

要考的是**读与联动**，不是「什么时候该写一条里程碑」（那由 `milestones.decide_milestone`
与评估器负责，另有专门的用例）。走评估链路意味着把模型替身、节流、频率窗口都拖进来，
一次失败要在四层里找原因。直接建一行段落 + 一条流水 + 一条引用，读侧看到的东西完全一样。

## 为什么用真表

这些性质全都是**数据层**的：`origin_kind` 的排除条件、`is_frozen` 的清理条件、
「涉及的人」跟着段落走、事务里的软删。用假存储验证等于把要保护的东西替换掉。

用例里的账号、群号、人设编号都是现编的占位值。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_instance_group import DBInstanceGroup
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.affection import (
    delete_affection_event_reason,
    delete_affection_milestone,
    get_affection_detail,
    reset_affection,
)
from nekro_agent.routers.memory_admin import get_prompt_round
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection import milestones as milestones_module
from nekro_agent.services.affection.evaluator import reset_evaluation_throttle
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.prompt import AFFECTION_BLOCK_KIND, render_affection_prompt
from nekro_agent.services.affection.stage import AffectionEventSource, stage_of
from nekro_agent.services.affection.store import invalidate_cache
from nekro_agent.services.affection.suppression import (
    lift_narrative_suppression,
    reset_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.identity_admin import (
    cleanup_orphan_preset,
    list_orphan_buckets,
    merge_self_domain_into_group,
)
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from nekro_agent.services.memory import admin_service, prompt_rounds
from nekro_agent.services.memory import maintenance as maintenance_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory import rebuild as rebuild_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.persons import replace_paragraph_persons
from nekro_agent.services.memory.retriever import (
    BatchRecallRequest,
    retrieve_memories,
    retrieve_memories_batch,
)
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE_A = "inst-alpha"
INSTANCE_B = "inst-rival"
TENANT_A = 11
TENANT_B = 22
PERSON = "acct-main"
PERSON_OTHER = "acct-other"
PERSON_RIVAL = "acct-rival"
CHAT_KEY = f"{ADAPTER}-{INSTANCE_A}-group_1"
ONE_TIME_CODE = "a1b2c3d4e5f60718"
WORKSPACE_ID = 0
MILESTONE_REASON = "那天一起熬夜赶工，聊了很久"


class _NullQdrant:
    """向量库替身：只记账，不连网。

    这些用例考的是**数据库侧**的取舍（来源排除、冻结跳过、软删），向量点不是被测对象。
    返回真实删除条数，免得 `_delete_vectors` 把它当成「删了一半」再打一条错误日志。
    """

    def __init__(self) -> None:
        self.deleted: List[int] = []

    async def upsert_paragraph(self, **_kwargs: Any) -> bool:
        return True

    async def delete_paragraphs(self, paragraph_ids: Sequence[int]) -> int:
        self.deleted.extend(int(item) for item in paragraph_ids)
        return len(list(paragraph_ids))

    async def delete_by_workspace(self, _workspace_id: int) -> int:
        return 0

    async def search(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        return []


class _ScriptedQdrant:
    """按段落 ID 返回固定相似度，**不做任何 payload 过滤**。

    过滤必须真的发生在 SQL 侧：替身自己过滤的话，用例考的是替身。
    """

    def __init__(self, scores: Dict[int, float]) -> None:
        self.scores = scores

    async def search(self, **kwargs: Any) -> List[Dict[str, Any]]:
        threshold = float(kwargs.get("score_threshold", 0.0))
        return [
            {"id": paragraph_id, "score": score, "payload": {}}
            for paragraph_id, score in sorted(self.scores.items(), key=lambda item: -item[1])
            if score >= threshold
        ]


class _Channel:
    """只提供写入轮次需要的频道字段，避免把频道配置合并拖进用例。"""

    def __init__(self, chat_key: str = CHAT_KEY, *, instance_key: str = INSTANCE_A, preset_id: int = PRESET_NONE) -> None:
        self.chat_key = chat_key
        self.adapter_key = ADAPTER
        self.instance_key = instance_key
        self.workspace_id = WORKSPACE_ID
        self.channel_type = "group"
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id

    async def get_effective_config(self) -> SimpleNamespace:
        return SimpleNamespace(MEMORY_ENABLE_FOR_CHAT=True)

    @property
    def chat_type(self) -> SimpleNamespace:
        return SimpleNamespace(value="group")


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    """内存 SQLite 上的真表结构 + 一份安静的向量库。

    注册**整个** `nekro_agent.models`：域解析要读实例组绑定表、人设回退链要读人设表，
    少注册一张就会让整条解析链路在测试里跑不起来——而它正是这些用例的起点。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)
    monkeypatch.setattr(config, "AFFECTION_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "AFFECTION_ENABLE_PROMPT_INJECTION", True)
    monkeypatch.setattr(config, "AFFECTION_MILESTONE_ENABLED", True)
    # 关掉好感快照缓存：这些用例几乎每条都会在渲染之后改库，开着缓存只会让断言读到旧值，
    # 而「缓存要不要失效」是另一组用例（后台三项操作）的事。
    monkeypatch.setattr(config, "AFFECTION_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.0)
    monkeypatch.setattr(qdrant_manager_module, "memory_qdrant_manager", _NullQdrant())
    invalidate_cache()
    reset_evaluation_throttle()
    reset_narrative_suppression()
    prompt_rounds.reset_prompt_rounds()
    try:
        for tenant_id, name in ((TENANT_A, "acme"), (TENANT_B, "rival")):
            await DBTenant.create(id=tenant_id, name=name, display_name=name)
        for instance_key, tenant_id in ((INSTANCE_A, TENANT_A), (INSTANCE_B, TENANT_B)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider=ADAPTER,
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        await prompt_rounds.drain()
        invalidate_cache()
        reset_evaluation_throttle()
        reset_narrative_suppression()
        prompt_rounds.reset_prompt_rounds()
        await Tortoise.close_connections()


def make_user(tenant_id: Optional[int]) -> DBUser:
    role = TenantRole.TENANT_ADMIN if tenant_id is not None else TenantRole.PLATFORM_OWNER
    return DBUser(
        username=f"admin-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin if tenant_id is not None else Role.Super,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=role.value,
    )


def admin_a() -> DBUser:
    return make_user(TENANT_A)


def platform_owner() -> DBUser:
    return make_user(None)


def key_of(instance_key: str, person: str = PERSON, preset_id: int = PRESET_NONE) -> AffectionKey:
    return AffectionKey.of_domain(
        ScopeDomain.of_instance(ADAPTER, instance_key),
        person_uid=person,
        preset_id=preset_id,
    )


def make_config(**overrides: Any) -> CoreConfig:
    base: Dict[str, Any] = {
        "AFFECTION_ENABLE_SYSTEM": True,
        "AFFECTION_ENABLE_PROMPT_INJECTION": True,
        "AFFECTION_MILESTONE_ENABLED": True,
        "AFFECTION_PROMPT_MAX_USERS": 5,
        "AFFECTION_CACHE_TTL_SECONDS": 0,
    }
    base.update(overrides)
    return config.model_copy(update=base)


async def seed_affection(
    key: AffectionKey,
    *,
    score: int = 45,
    state_text: str = "聊得挺投机",
    last_event_text: str = "一起赶完了一个东西",
    interaction_count: int = 3,
) -> DBAffection:
    """建一行好感；分数有值、有过互动，`has_relationship` 才会为真。"""
    return await DBAffection.create(
        **key.as_columns(),
        nickname="小同事",
        score=score,
        stage=stage_of(score).value,
        state_text=state_text,
        last_event_text=last_event_text,
        interaction_count=interaction_count,
    )


async def add_milestone(
    record: DBAffection,
    key: AffectionKey,
    *,
    content: str,
    summary: Optional[str] = None,
    stage_from: str = "acquainted",
    stage_to: str = "friendly",
    at: Optional[datetime] = None,
    reason: str = MILESTONE_REASON,
    persons: Sequence[str] = (PERSON,),
    frozen: bool = True,
    event_time: Optional[datetime] = None,
) -> Tuple[DBMemParagraph, DBAffectionEvent]:
    """建一段里程碑段落 + 一条指向它的流水，并把它挂到好感行的引用最前面

    段落、流水、引用三处是同一个东西的三个视角：段落给读侧取正文，流水记住「谁生成过它」
    （删除接口的归属判据），引用决定好感块显示哪一条。
    """
    moment = at or db_now()
    paragraph = await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        instance_key=record.instance_key,
        adapter_key=record.adapter_key,
        preset_id=record.preset_id,
        origin_chat_key=CHAT_KEY,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.EMOTION,
        content=content,
        summary=summary if summary is not None else content,
        event_time=event_time or moment,
        half_life_seconds=90 * 24 * 3600,
        is_frozen=frozen,
        origin_kind=OriginKind.AFFECTION,
        origin_ref="affection_event:seed",
    )
    event = await DBAffectionEvent.create(
        **key.as_columns(),
        source_platform_userid=PERSON,
        chat_key=CHAT_KEY,
        delta=8,
        score_after=record.score,
        source=AffectionEventSource.LLM.value,
        reason=reason,
        memory_paragraph_id=paragraph.id,
    )
    ref = {
        "paragraph_id": paragraph.id,
        "event_id": event.id,
        "from": stage_from,
        "to": stage_to,
        "at": moment.isoformat(),
    }
    record.milestone_refs = [ref, *list(record.milestone_refs or [])]
    await record.save(update_fields=["milestone_refs"])
    invalidate_cache(key)
    if persons:
        await replace_paragraph_persons(paragraph.id, list(persons))
    return paragraph, event


async def make_channel(instance_key: str = INSTANCE_A, chat_key: str = CHAT_KEY) -> DBChatChannel:
    channel = await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        chat_key=chat_key,
        preset_id=None,
        data="{}",
    )
    # 新建频道的对话起点就是「现在」，而好感只取当前对话里的参与者；往前挪两小时，
    # 让下面那条「半分钟前」的消息落在窗口里。
    channel.conversation_start_time = db_now() - timedelta(hours=2)
    await channel.save(update_fields=["conversation_start_time"])
    return channel


async def say(chat_key: str = CHAT_KEY, instance_key: str = INSTANCE_A, person: str = PERSON) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=person,
        sender_name="小同事",
        sender_nickname="小同事",
        is_tome=1,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=f"msg-{person}-1",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=person,
        content_text="在吗",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=int(db_now().timestamp()) - 30,
    )


async def render(channel: DBChatChannel, **overrides: Any) -> str:
    return await render_affection_prompt(
        db_chat_channel=channel,
        one_time_code=ONE_TIME_CODE,
        config=make_config(**overrides),
    )


def _block_bounds(rendered: str) -> Tuple[int, int]:
    return (
        rendered.index(f"<{ONE_TIME_CODE} | {AFFECTION_BLOCK_KIND}"),
        rendered.index(f"<{ONE_TIME_CODE} | /{AFFECTION_BLOCK_KIND}>"),
    )


# ---------------------------------------------------------------------------
# 一、好感块里的 `milestone:` 行
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestAffectionBlockShowsTheMilestone:
    async def test_one_line_inside_the_untrusted_block(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        await add_milestone(record, key, content="第一次一起熬夜\n我们聊到天亮", summary="第一次一起熬夜")
        channel = await make_channel()
        await say()

        rendered = await render(channel)

        # 正文里的换行被压成一行：里程碑造不出提示词里的新的一行，也就伪造不了边界
        assert "milestone: 第一次一起熬夜 我们聊到天亮" in rendered
        start, end = _block_bounds(rendered)
        line_at = rendered.index("milestone: ")
        assert start < line_at < end

    async def test_suppressed_narrative_also_drops_the_milestone(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        await add_milestone(record, key, content="第一次一起熬夜")
        channel = await make_channel()
        await say()
        assert "milestone:" in await render(channel)
        suppress_narrative(CHAT_KEY)
        try:
            rendered = await render(channel)
        finally:
            lift_narrative_suppression(CHAT_KEY)

        # 主对话刚被拦，这时再补一段没被审过的记忆只会让它继续被拦
        assert "milestone:" not in rendered
        assert "closeness:" in rendered

    async def test_soft_deleted_first_ref_does_not_fall_back_to_the_previous_one(self) -> None:
        """最近一条被删掉就不显示，不往后找

        引用是「新的在前」的栈：往后找等于把一条更久远、当时已经翻篇的关系重新顶到最前面。
        """
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        older, _older_event = await add_milestone(record, key, content="很久以前那次和好")
        newer, _newer_event = await add_milestone(record, key, content="最近这次长谈")
        channel = await make_channel()
        await say()
        rendered = await render(channel)
        assert "milestone: 最近这次长谈" in rendered
        assert "很久以前那次和好" not in rendered

        await DBMemParagraph.filter(id=newer.id).update(is_inactive=True)
        invalidate_cache(key)

        rendered = await render(channel)
        assert "milestone:" not in rendered
        # 更早那条仍然是有效的，但它不该被顶上来
        assert await DBMemParagraph.filter(id=older.id, is_inactive=False).exists()

    async def test_soft_deleted_paragraph_disappears(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        paragraph, _event = await add_milestone(record, key, content="第一次一起熬夜")
        channel = await make_channel()
        await say()
        assert "milestone:" in await render(channel)

        await DBMemParagraph.filter(id=paragraph.id).update(is_inactive=True)
        invalidate_cache()

        rendered = await render(channel)
        assert "milestone:" not in rendered
        assert "closeness:" in rendered

    async def test_disabled_flag_hides_it_without_touching_the_rest(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        channel = await make_channel()
        await say()
        baseline = await render(channel)
        await add_milestone(record, key, content="第一次一起熬夜")

        rendered = await render(channel, AFFECTION_MILESTONE_ENABLED=False)

        assert "milestone:" not in rendered
        # 关掉里程碑不能顺带改变块里其余的文字
        assert rendered == baseline

    async def test_a_row_without_a_milestone_renders_byte_identical(self) -> None:
        """没有任何里程碑时，提示词必须与「这一列根本没有这个值」逐字相同

        反向也验一次：引用指向一个已经不存在的段落（例如被真删），读侧取不到正文，
        这时同样不能多出空行、占位符或半截标签。
        """
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        channel = await make_channel()
        await say()
        baseline = await render(channel)
        assert "milestone:" not in baseline

        record.milestone_refs = [
            {
                "paragraph_id": 987654,
                "event_id": 987655,
                "from": "acquainted",
                "to": "friendly",
                "at": db_now().isoformat(),
            },
        ]
        await record.save(update_fields=["milestone_refs"])
        invalidate_cache(key)

        assert await render(channel) == baseline


# ---------------------------------------------------------------------------
# 二、自动记忆块回表时排除里程碑
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestAutoRecallExcludesMilestones:
    async def _seed_two_paragraphs(self) -> Tuple[DBMemParagraph, DBMemParagraph]:
        ordinary = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC,
            knowledge_type=KnowledgeType.FACT,
            content="普通的一段记忆：季度预算按部门拆好了",
            summary="季度预算",
            half_life_seconds=90 * 24 * 3600,
            origin_kind=OriginKind.CONSOLIDATION,
            origin_chat_key=CHAT_KEY,
        )
        milestone = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC,
            knowledge_type=KnowledgeType.EMOTION,
            content="里程碑：我们从上个月开始变得熟络",
            summary="变得熟络",
            half_life_seconds=90 * 24 * 3600,
            is_frozen=True,
            origin_kind=OriginKind.AFFECTION,
            origin_chat_key=CHAT_KEY,
        )
        return ordinary, milestone

    async def test_batch_recall_filters_affection_but_tools_recall_keeps_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ordinary, milestone = await self._seed_two_paragraphs()

        async def fake_embed(_text: str, **_kwargs: Any) -> List[float]:
            return [1.0, 0.0, 0.0, 0.0]

        monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
        monkeypatch.setattr(
            retriever_module,
            "memory_qdrant_manager",
            _ScriptedQdrant({ordinary.id: 0.93, milestone.id: 0.91}),
        )
        scope = MemoryScope.unrestricted()

        excluded = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query="我们最近怎么样", limit=10)],
            scope=scope,
            record_access=False,
            exclude_origin_kinds=(OriginKind.AFFECTION,),
        )
        kept = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="我们最近怎么样",
            limit=10,
            scope=scope,
            requester=None,
        )

        assert [memory.paragraph_id for memory in excluded[0]] == [ordinary.id]
        # 工具侧不传排除：用户问「我们是怎么熟起来的」时照样要搜得到
        assert {memory.paragraph_id for memory in kept} == {ordinary.id, milestone.id}


# ---------------------------------------------------------------------------
# 三、频道重建保留里程碑
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestChannelRebuildKeepsMilestones:
    async def test_only_the_ordinary_domain_row_and_its_persons_are_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ordinary = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            memory_source="na",
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=KnowledgeType.CONVERSATION,
            content="重建会回放出来的普通域行",
            summary="普通域行",
            half_life_seconds=7200,
            origin_kind=OriginKind.CONSOLIDATION,
            origin_chat_key=CHAT_KEY,
        )
        milestone = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC,
            knowledge_type=KnowledgeType.EMOTION,
            content="重建回放不出来的里程碑",
            summary="里程碑",
            half_life_seconds=90 * 24 * 3600,
            is_frozen=True,
            origin_kind=OriginKind.AFFECTION,
            origin_chat_key=CHAT_KEY,
        )
        await replace_paragraph_persons(ordinary.id, [PERSON])
        await replace_paragraph_persons(milestone.id, [PERSON])

        qdrant = _NullQdrant()
        monkeypatch.setattr(rebuild_module, "memory_qdrant_manager", qdrant)

        removed = await rebuild_module.reset_domain_channel_memory(CHAT_KEY)

        # 计数只算真正被删掉的普通段落：里程碑留着，不该被算进「重建清了 N 条」
        assert removed == 1
        assert not await DBMemParagraph.filter(id=ordinary.id).exists()
        assert await DBMemParagraphPerson.filter(paragraph_id=ordinary.id).count() == 0
        assert await DBMemParagraph.filter(id=milestone.id, is_inactive=False).exists()
        assert await DBMemParagraphPerson.filter(paragraph_id=milestone.id, account=PERSON).count() == 1
        assert ordinary.id in qdrant.deleted
        assert milestone.id not in qdrant.deleted


# ---------------------------------------------------------------------------
# 四、自动清理跳过冻结的里程碑
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestPruneSkipsFrozenMilestones:
    async def test_frozen_milestone_survives_even_when_its_weight_is_far_below_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        long_ago = db_now() - timedelta(days=4000)
        milestone = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC,
            knowledge_type=KnowledgeType.EMOTION,
            content="很久以前那条里程碑",
            summary="很久以前那条里程碑",
            event_time=long_ago,
            last_reinforced_at=long_ago,
            half_life_seconds=3600,
            # 基础权重本身就压在阈值以下：冻结让 `compute_effective_weight` 直接返回它，
            # 于是「这条该不该被清理」完全取决于清理查询里那道 `is_frozen=False`。
            # 只给一个久的 `event_time` 是不够的——冻结的行根本不衰减，用例会恒绿。
            base_weight=0.01,
            is_frozen=True,
            origin_kind=OriginKind.AFFECTION,
            origin_chat_key=CHAT_KEY,
        )
        ordinary = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            memory_source="na",
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=KnowledgeType.CONVERSATION,
            content="同样很久没被想起来的普通段落",
            summary="普通段落",
            event_time=long_ago,
            last_reinforced_at=long_ago,
            half_life_seconds=3600,
            origin_kind=OriginKind.CONSOLIDATION,
            origin_chat_key=CHAT_KEY,
        )
        monkeypatch.setattr(config, "MEMORY_PRUNE_PARAGRAPH_THRESHOLD", 0.05)
        monkeypatch.setattr(maintenance_module, "memory_qdrant_manager", _NullQdrant())

        now = db_now()
        pruned = await maintenance_module._prune_paragraphs(  # noqa: SLF001
            WORKSPACE_ID,
            now.timestamp(),
            now,
        )

        # 对照：同样的「很久以前」在普通段落上确实会衰减到阈值以下、真的被清理
        assert ordinary.id in pruned
        assert not await DBMemParagraph.filter(id=ordinary.id, is_inactive=False).exists()
        assert milestone.id not in pruned
        assert await DBMemParagraph.filter(id=milestone.id, is_inactive=False).exists()


# ---------------------------------------------------------------------------
# 五、被拦轮次计数与自动隔离
# ---------------------------------------------------------------------------


def _seed_quarantine_config(monkeypatch: pytest.MonkeyPatch, *, threshold: int) -> None:
    monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", threshold)
    monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 20)

    async def _keep_vectors(_ids: Sequence[int]) -> bool:
        return True

    monkeypatch.setattr(admin_service, "delete_paragraph_vectors", _keep_vectors)


async def _milestone_paragraph(content: str) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.EMOTION,
        content=content,
        summary=content,
        half_life_seconds=90 * 24 * 3600,
        is_frozen=True,
        origin_kind=OriginKind.AFFECTION,
        origin_chat_key=CHAT_KEY,
    )


def _run_blocked_round(code: str, paragraph_id: int) -> None:
    prompt_rounds.begin(code, _Channel())
    prompt_rounds.note_affection(code, shown=True, chars=120, people=1, milestone_ids=[paragraph_id])
    prompt_rounds.finish(code, "blocked")


@pytest.mark.usefixtures("db")
class TestBlockedRoundCountsMilestones:
    async def test_one_blocked_round_adds_one_hit_and_quarantines_at_the_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_quarantine_config(monkeypatch, threshold=2)
        paragraph = await _milestone_paragraph("被拦轮次里出现过的那条里程碑")

        _run_blocked_round("code-first", paragraph.id)
        await prompt_rounds.drain()

        first = await DBMemParagraph.get(id=paragraph.id)
        assert first.blocked_round_hits == 1
        assert first.is_inactive is False

        _run_blocked_round("code-second", paragraph.id)
        await prompt_rounds.drain()

        second = await DBMemParagraph.get(id=paragraph.id)
        assert second.blocked_round_hits == 2
        # 到阈值即转隔离：与记忆块里的段落同一套计数、同一套转隔离入口
        assert second.is_inactive is True
        assert second.quarantined_at is not None
        assert second.quarantine_reason == "auto_blocked"

    async def test_round_detail_reports_the_milestone_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_quarantine_config(monkeypatch, threshold=0)
        paragraph = await _milestone_paragraph("明细里要能看见的那条里程碑")
        prompt_rounds.begin("code-detail", _Channel())
        prompt_rounds.note_affection("code-detail", shown=True, chars=88, people=1, milestone_ids=[paragraph.id])
        prompt_rounds.finish("code-detail", "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        detail = await get_prompt_round(round_pk=row.id, _current_user=platform_owner())

        assert detail.affection_milestone_ids == [paragraph.id]
        assert detail.affection_shown is True

    async def test_the_real_render_path_records_the_id_and_feeds_the_blocked_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """从渲染到落库走一遍真实接线

        前两条用例直接调 `note_affection`，验的是计数与隔离；这一条验的是
        `render_affection_prompt` 真的把「这一轮渲染了哪一段」交了出去——漏传 `milestone_ids`
        时计数与隔离都照常工作，只有这条会红。
        """
        _seed_quarantine_config(monkeypatch, threshold=5)
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        paragraph, _event = await add_milestone(record, key, content="真实渲染路径上的里程碑")
        channel = await make_channel()
        await say()

        code = "code-real-render"
        prompt_rounds.begin(code, channel)
        rendered = await render_affection_prompt(
            db_chat_channel=channel,
            one_time_code=code,
            config=make_config(),
        )
        assert "milestone: 真实渲染路径上的里程碑" in rendered
        prompt_rounds.finish(code, "blocked")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert row.outcome == "blocked"
        assert row.affection_milestone_ids == [paragraph.id]
        refreshed = await DBMemParagraph.get(id=paragraph.id)
        assert refreshed.blocked_round_hits == 1


# ---------------------------------------------------------------------------
# 六、好感重置连带软删里程碑
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestResetClearsMilestonesOfThatRelationship:
    async def test_only_this_persons_milestones_are_soft_deleted(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        first, _e1 = await add_milestone(record, key, content="第一条里程碑")
        second, _e2 = await add_milestone(record, key, content="第二条里程碑")
        other_key = key_of(INSTANCE_A, PERSON_OTHER)
        other_record = await seed_affection(other_key)
        other_paragraph, _e3 = await add_milestone(other_record, other_key, content="别人的里程碑")

        await reset_affection(record_id=record.id, instance_key=INSTANCE_A, _current_user=admin_a())

        refreshed = await DBAffection.get(id=record.id)
        assert refreshed.milestone_refs == []
        assert refreshed.score == 0
        for paragraph in (first, second):
            row = await DBMemParagraph.get(id=paragraph.id)
            assert row.is_inactive is True
        untouched = await DBMemParagraph.get(id=other_paragraph.id)
        assert untouched.is_inactive is False
        other_refreshed = await DBAffection.get(id=other_record.id)
        assert len(other_refreshed.milestone_refs) == 1


# ---------------------------------------------------------------------------
# 七、删流水理由改写成摘要
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestRedactingTheReasonRewritesTheMilestone:
    async def test_content_falls_back_to_summary_and_the_action_is_recorded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _skip_embedding(_paragraph: DBMemParagraph, _accounts: Sequence[str]) -> None:
            return None

        monkeypatch.setattr(milestones_module, "_embed_and_upsert", _skip_embedding)
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        summary = "某月某日，Bot 和小同事的关系从「眼熟」变成了「熟络」。"
        paragraph, event = await add_milestone(
            record,
            key,
            content=f"{summary}起因：{MILESTONE_REASON}",
            summary=summary,
            reason=MILESTONE_REASON,
        )

        await delete_affection_event_reason(
            record_id=record.id,
            event_id=event.id,
            instance_key=INSTANCE_A,
            _current_user=admin_a(),
        )

        rewritten = await DBMemParagraph.get(id=paragraph.id)
        # 起因抄自那条理由，理由删了正文却留着等于换个地方原样保存
        assert rewritten.content == summary
        assert MILESTONE_REASON not in rewritten.content
        assert rewritten.last_manual_action == "affection_redact"
        assert rewritten.last_manual_action_at is not None


# ---------------------------------------------------------------------------
# 八、入域折叠时合并里程碑引用
# ---------------------------------------------------------------------------


def _ref(paragraph_id: int, at: str, event_id: int = 1) -> Dict[str, Any]:
    return {
        "paragraph_id": paragraph_id,
        "event_id": event_id,
        "from": "acquainted",
        "to": "friendly",
        "at": at,
    }


@pytest.mark.usefixtures("db")
class TestDomainFoldMergesMilestoneRefs:
    async def test_target_keeps_newest_five_deduped_by_paragraph(self) -> None:
        group = await DBInstanceGroup.create(name="acme 的域", note="", tenant_id=TENANT_A)
        target_domain = ScopeDomain.of_group(group.id)
        source_key = key_of(INSTANCE_A)
        target_key = AffectionKey.of_domain(target_domain, person_uid=PERSON, preset_id=PRESET_NONE)

        source = await seed_affection(source_key, score=20, state_text="低分一侧", last_event_text="低分近况")
        target = await seed_affection(target_key, score=50, state_text="高分一侧", last_event_text="高分近况")
        source.milestone_refs = [
            _ref(7011, "2026-01-11T10:00:00+00:00", event_id=101),
            _ref(7012, "2026-01-05T10:00:00+00:00", event_id=102),
            _ref(7013, "2025-12-20T10:00:00+00:00", event_id=103),
            _ref(7014, "2026-02-01T10:00:00+00:00", event_id=104),
        ]
        target.milestone_refs = [
            _ref(7021, "2026-03-01T10:00:00+00:00", event_id=201),
            # 与源侧同一段：去重只能留一条，留的是先遇到的那一条
            _ref(7012, "2026-04-01T10:00:00+00:00", event_id=202),
            _ref(7022, "2026-02-15T10:00:00+00:00", event_id=203),
        ]
        await source.save(update_fields=["milestone_refs"])
        await target.save(update_fields=["milestone_refs"])

        await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=INSTANCE_A, group=group, operator="ops")

        assert await DBAffection.filter(id=source.id).exists() is False
        folded = await DBAffection.get(id=target.id)
        ids = [ref["paragraph_id"] for ref in folded.milestone_refs]
        # 按 at 倒序、按段落去重、最多五条
        assert ids == [7021, 7022, 7014, 7011, 7012]
        times = [datetime.fromisoformat(ref["at"]) for ref in folded.milestone_refs]
        assert times == sorted(times, reverse=True)


# ---------------------------------------------------------------------------
# 九、孤儿人设清理连带软删里程碑
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestOrphanCleanupSoftDeletesMilestones:
    async def test_bucket_milestone_is_soft_deleted_and_counted(self) -> None:
        # 这个编号没有对应的人设行，因此这一桶确实是孤儿
        orphan_preset_id = 7301
        key = key_of(INSTANCE_A, preset_id=orphan_preset_id)
        record = await DBAffection.create(
            **key.as_columns(),
            nickname="小同事",
            score=30,
            stage=stage_of(30).value,
            interaction_count=2,
        )
        paragraph, _event = await add_milestone(record, key, content="孤儿桶里的里程碑")
        # `add_milestone` 按 PERSON 写「涉及的人」；这里不关心它，只关心段落状态

        buckets = [item for item in await list_orphan_buckets([(ADAPTER, INSTANCE_A)]) if item.preset_id == orphan_preset_id]
        assert len(buckets) == 1

        counts = await cleanup_orphan_preset(buckets[0])

        assert counts["milestones"] == 1
        assert counts["affection"] == 1
        assert counts["affection_event"] == 1
        row = await DBMemParagraph.get(id=paragraph.id)
        assert row.is_inactive is True
        assert row.last_manual_action == "delete"


# ---------------------------------------------------------------------------
# 十、后台详情与删除接口
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestDetailAndDeleteEndpoints:
    async def _seed(self) -> Tuple[DBAffection, DBMemParagraph, DBAffectionEvent]:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key)
        summary = "某月某日，Bot 和小同事的关系从「眼熟」变成了「熟络」。"
        paragraph, event = await add_milestone(
            record,
            key,
            content=f"{summary}起因：{MILESTONE_REASON}",
            summary=summary,
        )
        return record, paragraph, event

    async def test_detail_lists_the_expected_fields(self) -> None:
        record, paragraph, event = await self._seed()

        detail = await get_affection_detail(record_id=record.id, instance_key=INSTANCE_A, _current_user=admin_a())

        assert len(detail.milestones) == 1
        item = detail.milestones[0]
        assert item.paragraph_id == paragraph.id
        assert item.event_id == event.id
        assert item.content.startswith("某月某日")
        assert item.summary == "某月某日，Bot 和小同事的关系从「眼熟」变成了「熟络」。"
        assert item.event_time is not None
        assert item.status == "active"

    async def test_platform_delete_flips_the_status_to_deleted(self) -> None:
        record, paragraph, _event = await self._seed()

        response = await delete_affection_milestone(
            record_id=record.id,
            paragraph_id=paragraph.id,
            instance_key=INSTANCE_A,
            _current_user=platform_owner(),
        )
        assert response.ok is True

        detail = await get_affection_detail(record_id=record.id, instance_key=INSTANCE_A, _current_user=admin_a())
        (item,) = detail.milestones
        assert item.status == "deleted"
        # 引用不动：段落本身是记忆，删错了要能在记忆页找回来
        refreshed = await DBAffection.get(id=record.id)
        assert refreshed.milestone_refs[0]["paragraph_id"] == paragraph.id

    async def test_tenant_deleting_another_tenants_record_is_404(self) -> None:
        rival_key = key_of(INSTANCE_B, PERSON_RIVAL)
        rival = await seed_affection(rival_key)
        rival_paragraph, _event = await add_milestone(rival, rival_key, content="别人的里程碑")

        with pytest.raises(NotFoundError):
            await delete_affection_milestone(
                record_id=rival.id,
                paragraph_id=rival_paragraph.id,
                instance_key=INSTANCE_B,
                _current_user=admin_a(),
            )

        assert await DBMemParagraph.filter(id=rival_paragraph.id, is_inactive=False).exists()

    async def test_paragraph_of_another_person_is_404(self) -> None:
        record, _paragraph, _event = await self._seed()
        other_key = key_of(INSTANCE_A, PERSON_OTHER)
        other_record = await seed_affection(other_key)
        foreign, _foreign_event = await add_milestone(other_record, other_key, content="别人的里程碑")

        with pytest.raises(NotFoundError):
            await delete_affection_milestone(
                record_id=record.id,
                paragraph_id=foreign.id,
                instance_key=INSTANCE_A,
                _current_user=admin_a(),
            )

        assert await DBMemParagraph.filter(id=foreign.id, is_inactive=False).exists()
        # 别人的引用也不能被这次调用动到
        other_refreshed = await DBAffection.get(id=other_record.id)
        assert other_refreshed.milestone_refs[0]["paragraph_id"] == foreign.id
