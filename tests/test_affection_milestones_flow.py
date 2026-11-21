"""好感里程碑写入链路的端到端契约

钉的是「分数跨过阶段之后，宿主到底写了什么」：段落落在哪些列、流水怎么反指、引用怎么留、
冷却与回差怎么拦、运营动作与前提不满足时为什么一个字都不写。

夹具范式照抄 `tests/test_affection_system.py`：内存 SQLite 建真表、频道用替身、评估用假模型。
不另造一套的理由是同一份判据只能有一处实现，测试里的"等价替身"迟早会与真实现分叉。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection import milestones
from nekro_agent.services.affection.evaluator import evaluate_channel, reset_evaluation_throttle
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.stage import (
    AffectionEventSource,
    AffectionStage,
    stage_of,
)
from nekro_agent.services.affection.store import (
    AffectionSnapshot,
    AffectionWriteOutcome,
    apply_model_delta,
    clear_narrative,
    edit_narrative,
    invalidate_cache,
    set_score,
)
from nekro_agent.services.identity_scope import (
    PRESET_NONE,
    IdentityScope,
    ScopeDomain,
    resolve_identity_scope,
)

ADAPTER_KEY = "onebot_v11"
INSTANCE_KEY = "ob24680"
USER_ID = "24680"
NICKNAME = "小明"
CHAT_KEY = f"{ADAPTER_KEY}-{INSTANCE_KEY}-group_4242"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def flow_db():
    """内存 SQLite 上的真实表结构

    注册整个 `nekro_agent.models`：里程碑要落 `mem_paragraph` 与「涉及的人」两张表，
    判据还要读 `instance_domain_binding` / `identity_alias` / `presets`。
    """
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["nekro_agent.models"]},
    )
    await Tortoise.generate_schemas()
    invalidate_cache()
    reset_evaluation_throttle()
    milestones.reset_milestone_state()
    try:
        yield
    finally:
        milestones.reset_milestone_state()
        invalidate_cache()
        reset_evaluation_throttle()
        await Tortoise.close_connections()


@pytest.fixture
def memory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """记忆总开关与域分区闸都开着；关掉的那两条由用例自己再改回去"""
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)


@pytest.fixture
def embed_calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    """嵌入与向量的替身，顺便记下调用

    `_embed_and_upsert` 里是函数内 import，因此只能从模块属性上替。软删那一步会走
    `admin_service` 的 `_delete_vectors`，也一并替掉——它必须不碰真的 Qdrant。
    """
    from nekro_agent.services.memory import embedding_service
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    calls: Dict[str, List[Any]] = {"embed": [], "upsert": [], "delete": []}

    async def _embed(text: str, *, requester: Any = None) -> List[float]:
        calls["embed"].append({"text": text, "requester": requester})
        return [0.1, 0.2, 0.3]

    async def _upsert(*, paragraph_id: int, embedding: List[float], payload: Dict[str, Any]) -> bool:
        calls["upsert"].append({"paragraph_id": paragraph_id, "embedding": embedding, "payload": payload})
        return True

    async def _delete(ids: List[int]) -> int:
        calls["delete"].append(list(ids))
        return len(ids)

    monkeypatch.setattr(embedding_service, "embed_text", _embed)
    monkeypatch.setattr(memory_qdrant_manager, "upsert_paragraph", _upsert)
    monkeypatch.setattr(memory_qdrant_manager, "delete_paragraphs", _delete)
    return calls


def make_config(**overrides: Any) -> CoreConfig:
    """启用好感度与里程碑的一份配置，频道级记忆开关也打开"""
    base: Dict[str, Any] = {
        "AFFECTION_ENABLE_SYSTEM": True,
        "AFFECTION_MAX_DELTA_PER_ROUND": 5,
        "AFFECTION_UPDATE_MIN_INTERVAL_SECONDS": 0,
        "AFFECTION_CACHE_TTL_SECONDS": 0,
        "AFFECTION_MILESTONE_ENABLED": True,
        "AFFECTION_MILESTONE_COOLDOWN_HOURS": 24,
        "MEMORY_ENABLE_FOR_CHAT": True,
    }
    base.update(overrides)
    return config.model_copy(update=base)


class _StubChannel:
    """频道替身：只提供里程碑真正读到的那几个标识与配置

    不建真 `DBChatChannel` 行：`is_memory_enabled_for_channel` 只要
    `get_effective_config()`，`_origin_chat_type` 只要 `chat_type`，都不需要落库。
    """

    def __init__(self, *, memory_for_chat: bool = True, channel_type: str = "group", preset_id: int = PRESET_NONE) -> None:
        self.chat_key = CHAT_KEY
        self.adapter_key = ADAPTER_KEY
        self.instance_key = INSTANCE_KEY
        self.channel_type = channel_type
        self._preset_id = preset_id
        self._effective = make_config(MEMORY_ENABLE_FOR_CHAT=memory_for_chat)

    @property
    def chat_type(self) -> ChatType:
        return ChatType(self.channel_type)

    async def get_effective_config(self) -> CoreConfig:
        return self._effective

    async def get_effective_preset_id(self) -> int:
        return self._preset_id

    async def get_preset(self):
        return type("_Preset", (), {"name": "测试人设", "content": "你是一只话很多的猫。"})()


async def add_message(content: str, *, platform_userid: str = USER_ID, nickname: str = NICKNAME) -> DBChatMessage:
    """往当前频道塞一条真人消息；时间取当下，免得被上下文过期时间筛掉"""
    timestamp = int(time.time())
    return await DBChatMessage.create(
        sender_id=platform_userid,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=1,
        is_recalled=False,
        adapter_key=ADAPTER_KEY,
        instance_key=INSTANCE_KEY,
        message_id=f"m{platform_userid}{timestamp}",
        chat_key=CHAT_KEY,
        chat_type="group",
        platform_userid=platform_userid,
        content_text=content,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=timestamp,
    )


def stub_llm(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """让评估调用返回指定的原始响应体"""

    class _Response:
        response_content = payload if isinstance(payload, str) else json.dumps(payload)

    async def _fake_gen(**_kwargs: Any) -> Any:
        return _Response()

    from nekro_agent.services.agent import openai as openai_module

    monkeypatch.setattr(openai_module, "gen_openai_chat_response", _fake_gen)


async def prepare() -> Tuple[_StubChannel, CoreConfig, IdentityScope, AffectionKey]:
    """一套干净的环境：频道、配置、解析出来的作用域与好感键"""
    channel = _StubChannel()
    cfg = make_config()
    scope = await resolve_identity_scope(channel, USER_ID)  # type: ignore[arg-type]
    return channel, cfg, scope, AffectionKey.of_scope(scope)


async def model_write(
    key: AffectionKey,
    *,
    scope: IdentityScope,
    channel: _StubChannel,
    cfg: CoreConfig,
    delta: int,
    reason: str = "",
    nickname: str = NICKNAME,
    persona_name: str = "测试人设",
    originals: Sequence[str] = (),
    source: AffectionEventSource = AffectionEventSource.LLM,
    source_account: str = USER_ID,
) -> Tuple[AffectionSnapshot, AffectionWriteOutcome, Optional[int]]:
    """一次「模型写分 + 判里程碑」，与评估里那两步逐字同序

    直接调 `apply_model_delta` / `maybe_record` 而不走 `evaluate_channel`，是为了能指定
    任意的分数变化量与起因；评估链路本身由第一条用例单独钉住。
    """
    snapshot, outcome = await apply_model_delta(
        key,
        delta=delta,
        reason=reason,
        source=AffectionEventSource.LLM,
        chat_key=CHAT_KEY,
        nickname=nickname,
        source_platform_userid=source_account,
    )
    paragraph_id = await milestones.maybe_record(
        key=key,
        scope=scope,
        channel=channel,  # type: ignore[arg-type]
        outcome=outcome,
        source=source,
        reason=reason,
        nickname=nickname,
        persona_name=persona_name,
        originals=originals,
        config=cfg,
        source_account=source_account,
    )
    return snapshot, outcome, paragraph_id


async def shift_last_ref(key: AffectionKey, hours: int = 25) -> None:
    """把最近一条里程碑的时间往前挪，绕过冷却

    改数据比 monkeypatch 时钟可靠：`db_now()` 在多个模块里被各自 import，
    替一处就会漏一处，而判据读的正是引用里那个 `at`。
    """
    row = await DBAffection.get(**key.as_columns())
    refs = [dict(item) for item in row.milestone_refs]
    at = datetime.fromisoformat(refs[0]["at"])
    refs[0]["at"] = (at - timedelta(hours=hours)).isoformat()
    row.milestone_refs = refs
    await row.save(update_fields=["milestone_refs", "update_time"])
    invalidate_cache(key)


async def milestone_paragraphs() -> List[DBMemParagraph]:
    return await DBMemParagraph.filter(origin_kind=OriginKind.AFFECTION).order_by("id")


# ---------------------------------------------------------------------------
# 一、评估链路端到端
# ---------------------------------------------------------------------------


class TestEvaluationChain:
    async def test_score_29_to_30_writes_one_milestone(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        # 29 分是「眼熟」，30 分跨进「熟络」：这一步是唯一会写里程碑的地方
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        await add_message("今天被当众说了一顿，你陪我把话说完才走")
        stub_llm(
            monkeypatch,
            {"users": [{"user_id": USER_ID, "delta": 1, "reason": "他愿意把话听完", "feeling": "这个人挺耐心"}]},
        )

        applied = await evaluate_channel(channel, cfg)  # type: ignore[arg-type]
        assert applied == 1

        row = await DBAffection.get(**key.as_columns())
        assert row.score == 30
        assert len(row.milestone_refs) == 1
        ref = row.milestone_refs[0]
        assert set(ref) == {"paragraph_id", "event_id", "from", "to", "at"}

        paragraph = await DBMemParagraph.get(id=ref["paragraph_id"])
        event = await DBAffectionEvent.get(id=ref["event_id"])
        # 段落落在记忆表里，来源与归属逐项核对
        assert paragraph.origin_kind == OriginKind.AFFECTION
        assert paragraph.workspace_id == 0
        assert paragraph.instance_key == INSTANCE_KEY
        assert paragraph.adapter_key == ADAPTER_KEY
        assert paragraph.origin_chat_key == CHAT_KEY
        assert paragraph.origin_chat_type == "group"
        assert paragraph.preset_id == key.preset_id
        assert paragraph.cognitive_type == CognitiveType.SEMANTIC
        assert paragraph.knowledge_type == KnowledgeType.EMOTION
        assert paragraph.is_frozen is True
        assert paragraph.origin_ref == f"affection_event:{event.id}"
        # 摘要是完整的一句话，且不带起因：删流水理由时直接拿它覆盖正文
        assert "起因：" not in paragraph.summary
        assert "从「眼熟」变成了「熟络」" in paragraph.summary
        assert paragraph.content == paragraph.summary + "起因：他愿意把话听完"
        # 流水反指段落
        assert event.memory_paragraph_id == paragraph.id
        assert ref["event_id"] == event.id
        assert ref["from"] == AffectionStage.ACQUAINTED.value
        assert ref["to"] == AffectionStage.FRIENDLY.value
        assert datetime.fromisoformat(ref["at"]).tzinfo is not None
        # 「涉及的人」只有这一次说话的那个号
        accounts = await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).values_list("account", flat=True)
        assert accounts == [USER_ID]
        # 向量写成功了，引用记在段落上
        assert embed_calls["upsert"][-1]["paragraph_id"] == paragraph.id
        fresh = await DBMemParagraph.get(id=paragraph.id)
        assert fresh.embedding_ref == str(paragraph.id)


    async def test_person_row_is_the_speaking_account(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        """「涉及的人」写这一次说话的那个号，不是键上的那个「人」

        合并过的人有多个号：键上记的是主账号，而这条里程碑是副号说的。
        展开成主账号会让一条本该随副号撤销而消失的线索留在主账号名下。
        """
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        secondary = "24680-alt"
        _, _, paragraph_id = await model_write(
            key,
            scope=scope,
            channel=channel,
            cfg=cfg,
            delta=1,
            reason="副号也一起聊了很久",
            source_account=secondary,
        )
        assert paragraph_id is not None
        accounts = await DBMemParagraphPerson.filter(paragraph_id=paragraph_id).values_list("account", flat=True)
        assert accounts == [secondary]
        assert key.person_uid not in accounts


# ---------------------------------------------------------------------------
# 二、回差与冷却
# ---------------------------------------------------------------------------


class TestHysteresisAndCooldown:
    async def test_threshold_bounce_cooldown_and_backfill(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        _, _, first = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="第一次跨门槛")
        assert first is not None
        row = await DBAffection.get(**key.as_columns())
        assert len(row.milestone_refs) == 1
        assert (row.milestone_refs[0]["from"], row.milestone_refs[0]["to"]) == ("acquainted", "friendly")

        # 门槛附近来回跳：30 -> 29 -> 30 都在回差范围内，一条都不写
        await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=-1, reason="话不投机")
        await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="又聊回来了")
        row = await DBAffection.get(**key.as_columns())
        assert row.score == 30
        assert len(row.milestone_refs) == 1
        assert len(await milestone_paragraphs()) == 1

        # 冷却满 25 小时之后往上走：先到 50（仍在「熟络」内，不写），再到 60（熟络 -> 亲近）
        await shift_last_ref(key)
        await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=20, reason="一起打了几把游戏")
        row = await DBAffection.get(**key.as_columns())
        assert row.score == 50
        assert len(row.milestone_refs) == 1
        _, _, second = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=10, reason="愿意说心事了")
        assert second is not None
        row = await DBAffection.get(**key.as_columns())
        assert len(row.milestone_refs) == 2
        assert (row.milestone_refs[0]["from"], row.milestone_refs[0]["to"]) == ("friendly", "close")

        # 冷却内掉回 29：阶段已在回差之外，拦住它的只剩冷却
        await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=-31, reason="吵了一架")
        row = await DBAffection.get(**key.as_columns())
        assert row.score == 29
        assert len(row.milestone_refs) == 2
        assert len(await milestone_paragraphs()) == 2

        # 冷却过后下一次写分补上：亲近 -> 眼熟
        await shift_last_ref(key)
        _, _, third = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=0, reason="气消了")
        assert third is not None
        row = await DBAffection.get(**key.as_columns())
        assert len(row.milestone_refs) == 3
        assert (row.milestone_refs[0]["from"], row.milestone_refs[0]["to"]) == ("close", "acquainted")
        assert len(await milestone_paragraphs()) == 3


# ---------------------------------------------------------------------------
# 三、运营动作不写
# ---------------------------------------------------------------------------


class TestOperatorActions:
    async def test_manual_actions_and_manual_source_never_write(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        channel, cfg, scope, key = await prepare()

        # 人工调分跨过门槛、重置来源回到门槛下：阶段变了，但这不是关系里发生的事
        await set_score(key, score=29, reason="管理员调分", source=AffectionEventSource.MANUAL)
        await set_score(key, score=60, reason="管理员调分", source=AffectionEventSource.MANUAL)
        await set_score(
            key,
            score=29,
            reason="管理员重置",
            source=AffectionEventSource.RESET,
            clear_state_text=True,
        )
        # 只清印象 / 编辑印象
        await clear_narrative(key, source=AffectionEventSource.NARRATIVE_CLEAR, reason="管理员清空了印象")
        await edit_narrative(key, state_text="新印象", last_event_text="最近没什么事", operator="admin")

        assert await milestone_paragraphs() == []
        row = await DBAffection.get(**key.as_columns())
        assert row.milestone_refs == []

        # 直接以人工来源调用：门槛就在眼前，也必须返回 None
        _, outcome = await apply_model_delta(
            key,
            delta=1,
            reason="模型写分",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
        )
        blocked = await milestones.maybe_record(
            key=key,
            scope=scope,
            channel=channel,  # type: ignore[arg-type]
            outcome=outcome,
            source=AffectionEventSource.MANUAL,
            reason="模型写分",
            nickname=NICKNAME,
            persona_name="测试人设",
            originals=(),
            config=cfg,
            source_account=USER_ID,
        )
        assert blocked is None
        assert await milestone_paragraphs() == []

        # 同一份状态、同一个 outcome，只把来源换成模型写分就会写：证明差别只在来源
        allowed = await milestones.maybe_record(
            key=key,
            scope=scope,
            channel=channel,  # type: ignore[arg-type]
            outcome=outcome,
            source=AffectionEventSource.LLM,
            reason="模型写分",
            nickname=NICKNAME,
            persona_name="测试人设",
            originals=(),
            config=cfg,
            source_account=USER_ID,
        )
        assert allowed is not None
        assert len(await milestone_paragraphs()) == 1


# ---------------------------------------------------------------------------
# 四、起因
# ---------------------------------------------------------------------------


class TestReasonHandling:
    async def test_reason_copying_message_is_dropped(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """起因抄了本轮消息原文，整句丢掉：里程碑会进提示词，抄原文等于开一条注入通道"""
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        await add_message("今天下午我们把那只坏掉的台灯修好了，居然还能亮")
        stub_llm(
            monkeypatch,
            {"users": [{"user_id": USER_ID, "delta": 1, "reason": "把那只坏掉的台灯修好了", "feeling": "挺开心"}]},
        )

        assert await evaluate_channel(channel, cfg) == 1  # type: ignore[arg-type]
        paragraph = (await milestone_paragraphs())[0]
        assert paragraph.content == paragraph.summary
        assert "起因：" not in paragraph.content

    async def test_empty_reason_leaves_template_only(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        _, _, paragraph_id = await model_write(
            key,
            scope=scope,
            channel=channel,
            cfg=cfg,
            delta=1,
            reason="",
            originals=["今天天气不错"],
        )
        assert paragraph_id is not None
        paragraph = await DBMemParagraph.get(id=paragraph_id)
        assert paragraph.content == paragraph.summary
        assert "起因：" not in paragraph.content

    async def test_normal_reason_is_appended(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        _, _, paragraph_id = await model_write(
            key,
            scope=scope,
            channel=channel,
            cfg=cfg,
            delta=1,
            reason="他记得我说过喜欢桂花",
            originals=["今天下午我们把那只坏掉的台灯修好了"],
        )
        assert paragraph_id is not None
        paragraph = await DBMemParagraph.get(id=paragraph_id)
        assert paragraph.content.endswith("起因：他记得我说过喜欢桂花")
        assert paragraph.content.startswith(paragraph.summary)


# ---------------------------------------------------------------------------
# 五、嵌入失败
# ---------------------------------------------------------------------------


class TestEmbedFailure:
    async def test_paragraph_and_refs_survive_embed_error(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        from nekro_agent.services.memory import embedding_service

        async def _boom(text: str, *, requester: Any = None) -> List[float]:
            raise RuntimeError("嵌入服务不可用")

        monkeypatch.setattr(embedding_service, "embed_text", _boom)

        # 不抛：嵌入失败只让这条里程碑搜不到，段落本身照留
        _, _, paragraph_id = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="聊得不错")
        assert paragraph_id is not None

        paragraph = await DBMemParagraph.get(id=paragraph_id)
        assert paragraph.is_inactive is False
        row = await DBAffection.get(**key.as_columns())
        assert len(row.milestone_refs) == 1
        assert row.milestone_refs[0]["paragraph_id"] == paragraph_id
        assert embed_calls["upsert"] == []
        # 向量引用没写上：那一列的可空默认就是 None，嵌入失败时不该被填成任何值
        assert not paragraph.embedding_ref


# ---------------------------------------------------------------------------
# 六、前提不满足
# ---------------------------------------------------------------------------


class TestPrerequisites:
    @pytest.mark.parametrize(
        "blocker",
        ["milestone_off", "memory_off", "domain_gate_off", "channel_memory_off", "scope_mismatch"],
    )
    async def test_blocked_prerequisite_writes_nothing(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
        monkeypatch: pytest.MonkeyPatch,
        blocker: str,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        if blocker == "milestone_off":
            cfg = make_config(AFFECTION_MILESTONE_ENABLED=False)
        elif blocker == "memory_off":
            monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", False)
        elif blocker == "domain_gate_off":
            monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        elif blocker == "channel_memory_off":
            channel = _StubChannel(memory_for_chat=False)
        elif blocker == "scope_mismatch":
            # 好感域被覆盖成另一个域：写进去会落在与这份好感可见范围不一致的地方
            scope = scope._replace(affection_domain=ScopeDomain.of_instance(ADAPTER_KEY, "ob99999"))

        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        snapshot, outcome = await apply_model_delta(
            key,
            delta=1,
            reason="跨过门槛",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
        )
        # 先确认这一笔真的跨了阶段，否则 None 可能只是「没跨门槛」
        assert stage_of(snapshot.score) == AffectionStage.FRIENDLY
        assert outcome.stage_before == AffectionStage.ACQUAINTED

        result = await milestones.maybe_record(
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
        assert result is None
        assert await milestone_paragraphs() == []
        row = await DBAffection.get(**key.as_columns())
        assert row.milestone_refs == []
        assert embed_calls["upsert"] == []


# ---------------------------------------------------------------------------
# 七、并发
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_same_outcome_twice_concurrently_writes_one(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        _, outcome = await apply_model_delta(
            key,
            delta=1,
            reason="跨过门槛",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
        )

        def _call() -> Any:
            return milestones.maybe_record(
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

        results = await asyncio.gather(_call(), _call())
        assert sum(1 for item in results if item is not None) == 1
        assert len(await milestone_paragraphs()) == 1
        row = await DBAffection.get(**key.as_columns())
        assert len(row.milestone_refs) == 1


# ---------------------------------------------------------------------------
# 八、幂等
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_same_outcome_second_call_returns_none(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        _, outcome = await apply_model_delta(
            key,
            delta=1,
            reason="跨过门槛",
            source=AffectionEventSource.LLM,
            chat_key=CHAT_KEY,
            nickname=NICKNAME,
        )

        def _call() -> Any:
            return milestones.maybe_record(
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

        first = await _call()
        assert first is not None
        second = await _call()
        assert second is None
        assert len(await milestone_paragraphs()) == 1
        row = await DBAffection.get(**key.as_columns())
        assert len(row.milestone_refs) == 1


# ---------------------------------------------------------------------------
# 九、事务失败回滚
# ---------------------------------------------------------------------------


class TestRollback:
    async def test_reference_write_failure_soft_deletes_paragraph(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("写引用失败")

        # 引用落库那一步炸掉：段落已经建好，必须被软删，否则会留下一条谁也不指向、又能被检索到的记忆
        monkeypatch.setattr(milestones, "_merge_refs", _boom)

        _, outcome, paragraph_id = await model_write(
            key,
            scope=scope,
            channel=channel,
            cfg=cfg,
            delta=1,
            reason="跨过门槛",
        )
        assert paragraph_id is None

        paragraphs = await milestone_paragraphs()
        assert len(paragraphs) == 1
        assert paragraphs[0].is_inactive is True
        assert embed_calls["delete"] == [[paragraphs[0].id]]
        row = await DBAffection.get(**key.as_columns())
        assert row.milestone_refs == []
        event = await DBAffectionEvent.get(id=outcome.event_id)
        assert event.memory_paragraph_id is None

    async def test_event_backreference_failure_rolls_back_refs(
        self,
        flow_db: None,
        memory_on: None,
        embed_calls: Dict[str, List[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """流水反写与引用在同一个事务里：后一步失败时前一步必须一起回滚

        只回滚一半的话，好感行上会留下一条指向已软删段落的引用，提示词里就再也看不到
        里程碑了——而库里那一条其实是好的。
        """
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)

        real_events = milestones.DBAffectionEvent

        class _BrokenEvents:
            """只让「反写段落 ID」那一步失败；读流水、查重、查运营改分都照常走真表"""

            @staticmethod
            def filter(**kwargs: Any) -> Any:
                query = real_events.filter(**kwargs)

                class _Query:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(query, name)

                    async def update(self, **_kwargs2: Any) -> int:
                        raise RuntimeError("流水反写失败")

                return _Query()

        monkeypatch.setattr(milestones, "DBAffectionEvent", _BrokenEvents)

        _, _, paragraph_id = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="跨过门槛")
        assert paragraph_id is None
        assert embed_calls["upsert"] == []
        row = await DBAffection.get(**key.as_columns())
        assert row.milestone_refs == []
        paragraphs = await milestone_paragraphs()
        assert len(paragraphs) == 1
        assert paragraphs[0].is_inactive is True
