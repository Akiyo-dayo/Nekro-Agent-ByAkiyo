"""闸门关闭时的端到端黄金输出。

本文件只做一件事：在**改造产品代码之前**，用现有实现跑通检索、注入、工具、沉淀、调度五条
链路，把输出记进夹具。以后每一轮改记忆作用域，都拿新代码跑同一份布置，逐项与夹具比较。

为什么必须是端到端而不是只比对过滤条件：过滤条件对了，接到检索链路上的某一处漏传也会
让可见范围静默变大或变小。黄金输出把"条件、SQL、向量库、提示词编排"这四层一起钉住。

布置里所有会进输出的时间都取固定值；`create_time` / `update_time` / `last_reinforced_at`
这类由数据库或当前时钟写入的列在收集时剔除。段落行标成冻结权重，避免"当前时间"通过
衰减公式渗进 `effective_weight`。

沉淀提示词里每条消息前的 `[HH:MM]` 由生产代码按**进程本地时区**渲染（`fromtimestamp`
不带 tz，与主对话一致，部署时就是容器的时区）。录样本的机器与 CI 的时区不同就会差几个小时，
所以收集沉淀输出时把这一处钉成 UTC（`_UtcRenderedDatetime`），夹具里的时间是 UTC。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.services import message_service as message_service_module
from nekro_agent.services.agent.templates.history import _inject_memory_context
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory import semantic_writer as semantic_writer_module
from nekro_agent.services.memory.consolidator import consolidate_workspace
from nekro_agent.services.memory.instance_scope import resolve_memory_scope
from nekro_agent.services.memory.qdrant_manager import (
    MEMORY_PARAGRAPH_COLLECTION,
    memory_qdrant_manager,
)
from nekro_agent.services.memory.retriever import MemoryRetriever
from nekro_agent.services.memory.scheduler import MemoryMaintenanceScheduler
from plugins.builtin.memory_tools import main as memory_tools_module

_WORKSPACE_ID = 1
_PRESET_ONE_ID = 1
_PRESET_TWO_ID = 2
_ONE_TIME_CODE = "OTC0001"
_GOLDEN_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "memory_golden_gate_off.json"
_BASE_TIMESTAMP = 1_700_000
_MODEL_MODULES = [
    "nekro_agent.models.db_instance_group",
    "nekro_agent.models.db_identity",
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_mem_reinforcement_log",
    "nekro_agent.models.db_chat_channel",
    "nekro_agent.models.db_chat_message",
    "nekro_agent.models.db_workspace",
    "nekro_agent.models.db_preset",
]
_DYNAMIC_COLUMNS = {"create_time", "update_time", "last_reinforced_at", "last_manual_action_at"}
# 域分区新增的列：冻结基准生成时它们还不存在，因此模型行比较不包含它们。
# `origin_chat_key` 在 episode 上改造前就有，因此按表区分；新列的值由
# `tests/test_memory_scope_condition.py` 单独覆盖。
_GATE_OFF_NEW_COLUMNS = {
    "mem_paragraph": {
        "adapter_key",
        "origin_chat_type",
        "quarantined_at",
        "quarantine_reason",
        "blocked_round_hits",
        # 敏感标记只在闸开的域行上写，闸关恒为 false；取值由 `tests/test_memory_sensitive.py` 覆盖。
        "is_sensitive",
    },
    "mem_entity": {"adapter_key", "origin_chat_key"},
    "mem_relation": {"adapter_key", "origin_chat_key"},
    "mem_episode": {
        "adapter_key",
        "origin_chat_type",
        "quarantined_at",
        "quarantine_reason",
        "blocked_round_hits",
    },
}
# 夹具里的 Qdrant payload 只有改造前的那十个键；新键由 `test_memory_scope_condition.py` 钉住。
_GATE_OFF_QDRANT_PAYLOAD_KEYS = frozenset(
    {
        "workspace_id",
        "instance_key",
        "preset_id",
        "memory_source",
        "cognitive_type",
        "knowledge_type",
        "paragraph_id",
        "is_inactive",
        "event_time",
        "base_weight",
    }
)
_GATE_OFF_ADDED_QDRANT_PAYLOAD_KEYS = frozenset(
    {"adapter_key", "origin_chat_key", "origin_chat_type", "person_accounts", "payload_schema"}
)


@dataclass(frozen=True)
class _ChannelSpec:
    label: str
    chat_key: str
    channel_id: str
    channel_type: str
    instance_key: str
    workspace_id: int | None
    preset_id: int | None
    with_memory_state: bool = False


@dataclass(frozen=True)
class _ParagraphSpec:
    content: str
    instance_key: str
    preset_id: int
    origin_chat_key: str
    cognitive_type: CognitiveType
    knowledge_type: KnowledgeType
    base_weight: float
    event_offset_seconds: int
    episode_index: int | None = None
    is_inactive: bool = False


@dataclass(frozen=True)
class _EntitySpec:
    name: str
    canonical_name: str
    entity_type: EntityType
    instance_key: str
    preset_id: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class _RelationSpec:
    subject_index: int
    predicate: str
    object_index: int
    paragraph_index: int
    instance_key: str
    preset_id: int
    cognitive_type: str


@dataclass(frozen=True)
class _EpisodeSpec:
    title: str
    narrative_summary: str
    instance_key: str
    preset_id: int
    origin_chat_key: str
    start_offset_seconds: int
    end_offset_seconds: int
    paragraph_indices: tuple[int, ...]
    participant_entity_indices: tuple[int, ...]
    base_weight: float


@dataclass(frozen=True)
class _MessageSpec:
    sender_id: str
    sender_name: str
    sender_nickname: str
    platform_userid: str
    content_text: str
    second_offset: int


_CHANNEL_SPECS: tuple[_ChannelSpec, ...] = (
    _ChannelSpec("gA", "onebot_v11-ob1-group_101", "group_101", "group", "ob1", _WORKSPACE_ID, _PRESET_ONE_ID, True),
    _ChannelSpec("gB", "onebot_v11-ob1-group_102", "group_102", "group", "ob1", _WORKSPACE_ID, _PRESET_TWO_ID),
    _ChannelSpec("pX", "onebot_v11-ob1-private_103", "private_103", "private", "ob1", _WORKSPACE_ID, _PRESET_ONE_ID),
    _ChannelSpec("gU", "onebot_v11-ob1-group_104", "group_104", "group", "ob1", None, _PRESET_ONE_ID),
    _ChannelSpec("gC", "onebot_v11-ob2-group_105", "group_105", "group", "ob2", _WORKSPACE_ID, _PRESET_TWO_ID),
)

_PARAGRAPH_SPECS: tuple[_ParagraphSpec, ...] = (
    _ParagraphSpec(
        "甲频道记录了薄荷茶的配方",
        "ob1",
        _PRESET_ONE_ID,
        "gA",
        CognitiveType.SEMANTIC,
        KnowledgeType.FACT,
        1.0,
        400,
        episode_index=0,
    ),
    _ParagraphSpec(
        "乙频道讨论过周末徒步路线",
        "ob1",
        _PRESET_ONE_ID,
        "gB",
        CognitiveType.SEMANTIC,
        KnowledgeType.EXPERIENCE,
        0.96,
        300,
        episode_index=1,
    ),
    _ParagraphSpec(
        "私聊里确定了项目代号蓝鲸",
        "ob1",
        _PRESET_ONE_ID,
        "pX",
        CognitiveType.SEMANTIC,
        KnowledgeType.DECISION,
        0.92,
        250,
    ),
    _ParagraphSpec(
        "丙频道记录了橙子蛋糕的做法",
        "ob2",
        _PRESET_TWO_ID,
        "gC",
        CognitiveType.SEMANTIC,
        KnowledgeType.FACT,
        0.88,
        200,
        episode_index=2,
    ),
    _ParagraphSpec(
        "控制台记下用户喜欢清晨跑步",
        "",
        0,
        "__user__",
        CognitiveType.SEMANTIC,
        KnowledgeType.PREFERENCE,
        0.84,
        150,
    ),
    _ParagraphSpec(
        "旧实例留下的会议纪要提到薄荷茶",
        "",
        0,
        "gA",
        CognitiveType.EPISODIC,
        KnowledgeType.CONVERSATION,
        0.8,
        100,
    ),
    _ParagraphSpec(
        "甲频道上周决定把发布改到周五",
        "ob1",
        _PRESET_ONE_ID,
        "gA",
        CognitiveType.EPISODIC,
        KnowledgeType.DECISION,
        0.76,
        420,
    ),
    _ParagraphSpec(
        "甲频道有人提到灯塔计划",
        "ob1",
        _PRESET_TWO_ID,
        "gA",
        CognitiveType.SEMANTIC,
        KnowledgeType.FACT,
        0.72,
        50,
    ),
    _ParagraphSpec(
        "乙频道讨论蓝鲸项目的接口",
        "ob1",
        _PRESET_ONE_ID,
        "gB",
        CognitiveType.SEMANTIC,
        KnowledgeType.DECISION,
        0.68,
        310,
        episode_index=1,
    ),
    _ParagraphSpec(
        "丙频道里有条已失活的旧记忆",
        "ob2",
        _PRESET_TWO_ID,
        "gC",
        CognitiveType.EPISODIC,
        KnowledgeType.EXPERIENCE,
        0.64,
        210,
        is_inactive=True,
    ),
    _ParagraphSpec(
        "甲频道提到橙子蛋糕和下午茶",
        "ob1",
        _PRESET_ONE_ID,
        "gA",
        CognitiveType.EPISODIC,
        KnowledgeType.CONVERSATION,
        0.6,
        410,
        episode_index=0,
    ),
    _ParagraphSpec(
        "私聊里说周末要去看灯塔",
        "ob1",
        _PRESET_ONE_ID,
        "pX",
        CognitiveType.SEMANTIC,
        KnowledgeType.EXPERIENCE,
        0.56,
        260,
    ),
    _ParagraphSpec(
        "丙频道记录了蓝鲸项目的排期",
        "ob2",
        _PRESET_TWO_ID,
        "gC",
        CognitiveType.SEMANTIC,
        KnowledgeType.DECISION,
        0.52,
        220,
    ),
    _ParagraphSpec(
        "乙频道有人分享薄荷茶的做法",
        "ob1",
        _PRESET_TWO_ID,
        "gB",
        CognitiveType.SEMANTIC,
        KnowledgeType.FACT,
        0.48,
        320,
    ),
)

_ENTITY_SPECS: tuple[_EntitySpec, ...] = (
    _EntitySpec("薄荷茶", "薄荷茶", EntityType.CONCEPT, "", 0, ("薄荷饮品",)),
    _EntitySpec("蓝鲸项目", "蓝鲸项目", EntityType.PROJECT, "ob1", _PRESET_ONE_ID, ("蓝鲸",)),
    _EntitySpec("橙子蛋糕", "橙子蛋糕", EntityType.ARTIFACT, "ob2", _PRESET_TWO_ID, ("橙子糕",)),
)

_RELATION_SPECS: tuple[_RelationSpec, ...] = (
    _RelationSpec(0, "关联", 1, 0, "", 0, CognitiveType.SEMANTIC.value),
    _RelationSpec(1, "依赖", 0, 2, "ob1", _PRESET_ONE_ID, CognitiveType.SEMANTIC.value),
    _RelationSpec(2, "需要", 0, 3, "ob2", _PRESET_TWO_ID, CognitiveType.SEMANTIC.value),
)

_EPISODE_SPECS: tuple[_EpisodeSpec, ...] = (
    _EpisodeSpec(
        "薄荷茶讨论",
        "围绕薄荷茶配方与冲泡水温的讨论",
        "ob1",
        _PRESET_ONE_ID,
        "gA",
        400,
        410,
        (0, 10),
        (0, 1),
        0.9,
    ),
    _EpisodeSpec(
        "蓝鲸项目排期",
        "蓝鲸项目排期与接口安排的讨论",
        "ob1",
        _PRESET_ONE_ID,
        "gB",
        300,
        310,
        (1, 8),
        (1,),
        0.85,
    ),
    _EpisodeSpec(
        "橙子蛋糕做法",
        "橙子蛋糕与下午茶的制作记录",
        "ob2",
        _PRESET_TWO_ID,
        "gC",
        200,
        210,
        (3,),
        (2,),
        0.8,
    ),
)

_MESSAGE_SPECS: tuple[_MessageSpec, ...] = (
    _MessageSpec("u101", "小满", "小满", "u101", "薄荷茶配方里水温要低一些", 0),
    _MessageSpec("u102", "阿青", "阿青", "u102", "周末徒步路线还没有定", 60),
    _MessageSpec("u101", "小满", "小满", "u101", "蓝鲸项目排期放在下周", 120),
    _MessageSpec("-1", "助手", "助手", "bot", "我会把薄荷茶配方记下来", 180),
    _MessageSpec("u103", "小禾", "小禾", "u103", "橙子蛋糕可以加一点柠檬", 240),
    _MessageSpec("u102", "阿青", "阿青", "u102", "灯塔那边周末人多", 300),
    _MessageSpec("u101", "小满", "小满", "u101", "蓝鲸项目接口先做查询", 360),
    _MessageSpec("-1", "助手", "助手", "bot", "发布改到周五这件事我记下了", 420),
)

_RETRIEVAL_QUERIES: dict[str, tuple[str, ...]] = {
    "gA": ("薄荷茶 配方", "蓝鲸项目 排期"),
    "gC": ("橙子蛋糕 做法", "蓝鲸项目 排期"),
    "pX": ("灯塔 周末", "发布 周五"),
}

_FAKE_LLM_RESPONSE = json.dumps(
    {
        "memories": [
            {
                "content": "甲频道确认紫苏负责薄荷茶配方归档",
                "summary": "紫苏负责归档薄荷茶配方",
                "knowledge_type": "fact",
                "entities": ["紫苏", "薄荷茶配方"],
                "relations": [{"subject": "紫苏", "predicate": "负责", "object": "薄荷茶配方"}],
            },
            {
                "content": "甲频道决定把青柠加入蓝鲸项目排期",
                "summary": "青柠加入蓝鲸项目排期",
                "knowledge_type": "decision",
                "entities": ["青柠", "蓝鲸项目"],
                "relations": [{"subject": "青柠", "predicate": "加入", "object": "蓝鲸项目"}],
            },
        ]
    },
    ensure_ascii=False,
)

_EMBED_KEYWORDS: tuple[tuple[str, ...], ...] = (
    ("薄荷茶", "配方", "茶"),
    ("蓝鲸", "项目", "排期", "接口"),
    ("橙子", "蛋糕", "下午茶"),
    ("灯塔", "周末", "徒步", "跑步", "发布", "周五"),
)


def _embedding_for(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [0.02 + digest[index] / 255 * 0.02 for index in range(4)]
    for index, keywords in enumerate(_EMBED_KEYWORDS):
        if any(keyword in text for keyword in keywords):
            vector[index] = 1.0
    return vector


async def _fake_embed(text: str, **_kwargs: object) -> list[float]:
    return _embedding_for(text)


async def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
    return [_embedding_for(text) for text in texts]


@dataclass
class _GoldenEnv:
    workspace: DBWorkspace
    channels: dict[str, DBChatChannel]
    messages: list[DBChatMessage]
    paragraphs: list[DBMemParagraph]
    qdrant_client: AsyncQdrantClient


class _StubCtx:
    """只实现 `memory_tools` 用到的两个口子：绑定工作区与当前频道。"""

    def __init__(self, workspace: DBWorkspace | None, channel: DBChatChannel) -> None:
        self._workspace = workspace
        self.db_chat_channel = channel
        self.from_chat_key = channel.chat_key

    async def get_bound_workspace(self) -> DBWorkspace | None:
        return self._workspace


def _fixed_datetime(offset_seconds: int) -> datetime:
    return datetime.fromtimestamp(_BASE_TIMESTAMP + offset_seconds, tz=timezone.utc)


def _model_row(model: Any) -> dict[str, Any]:
    row: dict[str, Any] = {}
    new_columns = _GATE_OFF_NEW_COLUMNS.get(model._meta.db_table, set())
    for field_name in model._meta.fields_map:
        if field_name in _DYNAMIC_COLUMNS or field_name in new_columns:
            continue
        value = getattr(model, field_name)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, datetime):
            value = value.isoformat()
        row[field_name] = value
    return row


def _without_dynamic_memory_state(memory_state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in memory_state.items()
        if not key.endswith("_at") and key != "consolidation_retry_after"
    }


async def _max_id(model: Any) -> int:
    row = await model.all().order_by("-id").first()
    return int(row.id) if row is not None else 0


@pytest.fixture
async def golden_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_GoldenEnv]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(retriever_module, "embed_text", _fake_embed)
    monkeypatch.setattr(consolidator_module, "embed_text", _fake_embed)
    monkeypatch.setattr(semantic_writer_module, "embed_text", _fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_text", _fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", _fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", _fake_embed_batch)

    qdrant_client = AsyncQdrantClient(location=":memory:")

    async def fake_get_qdrant_client() -> AsyncQdrantClient:
        return qdrant_client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_qdrant_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": _MODEL_MODULES})
    await Tortoise.generate_schemas()
    try:
        workspace = await DBWorkspace.create(
            id=_WORKSPACE_ID,
            name="黄金输出工作区",
            description="端到端黄金输出的固定布置",
            status="active",
        )
        await DBPreset.create(
            id=_PRESET_ONE_ID,
            name="薄荷人设",
            title="薄荷人设",
            avatar="",
            content="你是薄荷，说话简洁。",
            description="黄金输出使用的人设一",
            tags="测试",
            author="测试",
        )
        await DBPreset.create(
            id=_PRESET_TWO_ID,
            name="蓝鲸人设",
            title="蓝鲸人设",
            avatar="",
            content="你是蓝鲸，说话温和。",
            description="黄金输出使用的人设二",
            tags="测试",
            author="测试",
        )

        channels: dict[str, DBChatChannel] = {}
        for spec in _CHANNEL_SPECS:
            data: dict[str, Any] = {}
            if spec.with_memory_state:
                data = {"memory_state": {"last_consolidated_message_db_id": 0}}
            channels[spec.label] = await DBChatChannel.create(
                chat_key=spec.chat_key,
                adapter_key="onebot_v11",
                instance_key=spec.instance_key,
                channel_id=spec.channel_id,
                channel_name=spec.label,
                channel_type=spec.channel_type,
                is_active=True,
                workspace_id=spec.workspace_id,
                preset_id=spec.preset_id,
                data=json.dumps(data, ensure_ascii=False),
            )

        messages: list[DBChatMessage] = []
        for index, message_spec in enumerate(_MESSAGE_SPECS, 1):
            messages.append(
                await DBChatMessage.create(
                    message_id=f"m{index:03d}",
                    sender_id=message_spec.sender_id,
                    sender_name=message_spec.sender_name,
                    sender_nickname=message_spec.sender_nickname,
                    platform_userid=message_spec.platform_userid,
                    is_tome=1 if message_spec.sender_id != "-1" else 0,
                    is_recalled=False,
                    adapter_key="onebot_v11",
                    instance_key="ob1",
                    chat_key=channels["gA"].chat_key,
                    chat_type="group",
                    content_text=message_spec.content_text,
                    content_data="[]",
                    raw_cq_code="",
                    ext_data="{}",
                    send_timestamp=_BASE_TIMESTAMP + message_spec.second_offset,
                )
            )

        paragraphs: list[DBMemParagraph] = []
        for spec in _PARAGRAPH_SPECS:
            paragraphs.append(
                await DBMemParagraph.create(
                    workspace_id=_WORKSPACE_ID,
                    instance_key=spec.instance_key,
                    preset_id=spec.preset_id,
                    memory_source="na",
                    cognitive_type=spec.cognitive_type,
                    knowledge_type=spec.knowledge_type,
                    content=spec.content,
                    summary=spec.content,
                    event_time=_fixed_datetime(spec.event_offset_seconds),
                    base_weight=spec.base_weight,
                    half_life_seconds=30 * 24 * 3600,
                    is_inactive=spec.is_inactive,
                    is_frozen=True,
                    origin_kind=OriginKind.CONSOLIDATION,
                    origin_chat_key=channels[spec.origin_chat_key].chat_key
                    if spec.origin_chat_key in channels
                    else spec.origin_chat_key,
                )
            )

        entities: list[DBMemEntity] = []
        for spec in _ENTITY_SPECS:
            entities.append(
                await DBMemEntity.create(
                    workspace_id=_WORKSPACE_ID,
                    instance_key=spec.instance_key,
                    preset_id=spec.preset_id,
                    entity_type=spec.entity_type,
                    name=spec.name,
                    canonical_name=spec.canonical_name,
                    aliases=list(spec.aliases),
                    source_hint="na",
                )
            )

        for relation_index, spec in enumerate(_RELATION_SPECS):
            relation = await DBMemRelation.create(
                workspace_id=_WORKSPACE_ID,
                instance_key=spec.instance_key,
                preset_id=spec.preset_id,
                subject_entity_id=entities[spec.subject_index].id,
                predicate=spec.predicate,
                object_entity_id=entities[spec.object_index].id,
                paragraph_id=paragraphs[spec.paragraph_index].id,
                memory_source="na",
                cognitive_type=spec.cognitive_type,
                base_weight=0.0,
                half_life_seconds=24 * 3600,
            )
            # 检索按 `update_time` 倒序取关系。自动写入的时间戳精度到秒，同一秒内建的多行
            # 会并列，排序就退化成数据库的返回顺序。这里把它们钉成互不相同的固定值，
            # 让并列关系也有确定的先后。
            await DBMemRelation.filter(id=relation.id).update(
                update_time=_fixed_datetime(1000 + relation_index),
            )

        episodes: list[DBMemEpisode] = []
        for spec in _EPISODE_SPECS:
            episodes.append(
                await DBMemEpisode.create(
                    workspace_id=_WORKSPACE_ID,
                    instance_key=spec.instance_key,
                    preset_id=spec.preset_id,
                    origin_chat_key=channels[spec.origin_chat_key].chat_key,
                    title=spec.title,
                    narrative_summary=spec.narrative_summary,
                    time_start=_fixed_datetime(spec.start_offset_seconds),
                    time_end=_fixed_datetime(spec.end_offset_seconds),
                    participant_entity_ids=[entities[index].id for index in spec.participant_entity_indices],
                    paragraph_ids=[paragraphs[index].id for index in spec.paragraph_indices],
                    base_weight=spec.base_weight,
                )
            )

        for paragraph_index, spec in enumerate(_PARAGRAPH_SPECS):
            if spec.episode_index is None:
                continue
            paragraphs[paragraph_index].episode_id = episodes[spec.episode_index].id
            await paragraphs[paragraph_index].save(update_fields=["episode_id", "update_time"])

        await memory_qdrant_manager.ensure_collection(4)
        for paragraph in paragraphs:
            await memory_qdrant_manager.upsert_paragraph(
                paragraph_id=paragraph.id,
                embedding=_embedding_for(paragraph.content),
                payload=paragraph.to_qdrant_payload(),
            )

        yield _GoldenEnv(
            workspace=workspace,
            channels=channels,
            messages=messages,
            paragraphs=paragraphs,
            qdrant_client=qdrant_client,
        )
    finally:
        await qdrant_client.close()
        await Tortoise.close_connections()


async def _collect_retrieve(env: _GoldenEnv) -> dict[str, list[list[Any]]]:
    collected: dict[str, list[list[Any]]] = {}
    for channel_label, queries in _RETRIEVAL_QUERIES.items():
        channel = env.channels[channel_label]
        for query in queries:
            scope = await resolve_memory_scope(channel)
            result = await MemoryRetriever(_WORKSPACE_ID, scope=scope).retrieve(query, record_access=False)
            items = [
                [memory.source_type, memory.target_id, round(memory.effective_weight, 6)]
                for memory in result.memories
            ]
            assert items, f"检索没有命中任何记忆: {channel_label} / {query}"
            collected[f"{channel_label}::{query}"] = items
    return collected


async def _collect_inject(env: _GoldenEnv) -> dict[str, str]:
    bound = await _inject_memory_context(
        workspace_id=_WORKSPACE_ID,
        recent_messages=env.messages,
        channel=env.channels["gA"],
        one_time_code=_ONE_TIME_CODE,
    )
    unbound = await _inject_memory_context(
        workspace_id=None,
        recent_messages=env.messages,
        channel=env.channels["gU"],
        one_time_code=_ONE_TIME_CODE,
    )
    assert bound, "绑定工作区的频道应当注入非空记忆块"
    assert unbound == "", "未绑定工作区的频道应当返回空串"
    return {"gA": bound, "gU": unbound}


async def _collect_tools(env: _GoldenEnv) -> dict[str, dict[str, Any]]:
    bound_ctx = _StubCtx(env.workspace, env.channels["gA"])
    unbound_ctx = _StubCtx(None, env.channels["gU"])

    bound_methods = await memory_tools_module.collect_memory_methods(bound_ctx)  # noqa: SLF001
    bound_search = await memory_tools_module.search_workspace_memories_tool(
        bound_ctx,  # noqa: SLF001
        query="薄荷茶 配方",
        limit=5,
    )
    unbound_methods = await memory_tools_module.collect_memory_methods(unbound_ctx)  # noqa: SLF001
    unbound_prompt = await memory_tools_module.memory_tools_prompt(unbound_ctx)  # noqa: SLF001
    try:
        await memory_tools_module.search_workspace_memories_tool(
            unbound_ctx,  # noqa: SLF001
            query="薄荷茶 配方",
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 - 这里记录的就是未绑定时的异常形态
        unbound_error: dict[str, str] | None = {"type": type(exc).__name__, "message": str(exc)}
    else:
        unbound_error = None

    return {
        "gA": {
            "prompt": await memory_tools_module.memory_tools_prompt(bound_ctx),  # noqa: SLF001
            "methods": [method.__name__ for method in bound_methods],
            "search_body": bound_search.body,
        },
        "gU": {
            "prompt": unbound_prompt,
            "methods": [method.__name__ for method in unbound_methods],
            "search_error": unbound_error,
        },
    }


class _UtcRenderedDatetime(datetime):
    """`fromtimestamp` 不带 tz 时按 UTC 而不是运行机器的本地时区渲染

    只换掉沉淀模块里的 `datetime`：生产代码用 `datetime.fromtimestamp(send_timestamp)` 给对话
    文本标时间，结果随运行机器的时区变；黄金输出必须与在哪台机器上跑无关。带 tz 的调用照常。
    """

    @classmethod
    def fromtimestamp(cls, t: float, tz: Any = None) -> datetime:  # type: ignore[override]
        if tz is None:
            return datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)
        return datetime.fromtimestamp(t, tz)


async def _collect_consolidate(env: _GoldenEnv, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(consolidator_module, "datetime", _UtcRenderedDatetime)
    prompts: list[str] = []

    async def fake_call_llm(
        _self: Any,
        prompt: str,
        *,
        use_fallback_model: bool = False,
    ) -> str:
        del use_fallback_model
        prompts.append(prompt)
        return _FAKE_LLM_RESPONSE

    monkeypatch.setattr(consolidator_module.EpisodicConsolidator, "_call_llm", fake_call_llm)

    before = {
        "paragraph": await _max_id(DBMemParagraph),
        "entity": await _max_id(DBMemEntity),
        "relation": await _max_id(DBMemRelation),
        "episode": await _max_id(DBMemEpisode),
    }

    result = await consolidate_workspace(_WORKSPACE_ID, env.channels["gA"].chat_key)
    assert result.paragraphs_created == 2
    assert len(prompts) == 1

    new_paragraphs = await DBMemParagraph.filter(id__gt=before["paragraph"]).order_by("id")
    new_entities = await DBMemEntity.filter(id__gt=before["entity"]).order_by("id")
    new_relations = await DBMemRelation.filter(id__gt=before["relation"]).order_by("id")
    new_episodes = await DBMemEpisode.filter(id__gt=before["episode"]).order_by("id")
    assert len(new_entities) == 4
    assert len(new_relations) == 2
    assert len(new_episodes) == 1

    records = await env.qdrant_client.retrieve(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        ids=[paragraph.id for paragraph in new_paragraphs],
        with_payload=True,
    )
    payloads: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda record: int(record.id)):
        payload = dict(record.payload or {})
        # 夹具只冻结改造前就有的键；新键单独断言，免得把「新增」误判成「旧值变了」。
        extra_keys = set(payload) - _GATE_OFF_QDRANT_PAYLOAD_KEYS
        assert extra_keys == _GATE_OFF_ADDED_QDRANT_PAYLOAD_KEYS, (
            f"Qdrant payload 新增键与预期不一致: {sorted(extra_keys)}"
        )
        payloads.append({key: payload[key] for key in _GATE_OFF_QDRANT_PAYLOAD_KEYS})

    channel = await DBChatChannel.get(chat_key=env.channels["gA"].chat_key)
    channel_data = json.loads(channel.data or "{}")
    memory_state = channel_data.get("memory_state", {})
    assert isinstance(memory_state, dict)

    return {
        "prompt": prompts[0],
        "paragraphs": [_model_row(paragraph) for paragraph in new_paragraphs],
        "entities": [_model_row(entity) for entity in new_entities],
        "relations": [_model_row(relation) for relation in new_relations],
        "episodes": [_model_row(episode) for episode in new_episodes],
        "qdrant_payloads": payloads,
        "memory_state": _without_dynamic_memory_state(memory_state),
    }


async def _collect_schedule(env: _GoldenEnv, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    scheduler = MemoryMaintenanceScheduler()
    handler_calls: list[tuple[int, str]] = []

    def handler(workspace_id: int, chat_key: str) -> asyncio.Task:
        handler_calls.append((workspace_id, chat_key))
        return asyncio.create_task(asyncio.sleep(0))

    scheduler.set_consolidation_handler(handler)
    monkeypatch.setattr(scheduler_module, "memory_scheduler", scheduler)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 2)

    for _ in range(3):
        await message_service_module._notify_memory_scheduler(  # noqa: SLF001
            channel=env.channels["gA"],
            content_length=10,
        )
        await message_service_module._notify_memory_scheduler(  # noqa: SLF001
            channel=env.channels["gU"],
            content_length=10,
        )

    channel_stats_keys = [list(key) for key in sorted(scheduler._channel_stats)]  # noqa: SLF001
    heap = [
        list(item)
        for item in sorted(
            (task.priority.name, task.workspace_id, task.chat_key) for task in scheduler.queued_tasks()
        )
    ]
    assert channel_stats_keys == [[_WORKSPACE_ID, env.channels["gA"].chat_key]]
    # 同一频道在队列里只留一项：三次触发只更新它，不再重复入队
    assert len(heap) == 1
    assert handler_calls == []
    return {"channel_stats_keys": channel_stats_keys, "heap": heap}


def _is_regen() -> bool:
    return os.environ.get("NA_GOLDEN_REGEN") == "1"


def _load_fixture() -> dict[str, Any]:
    if not _GOLDEN_FIXTURE_PATH.exists():
        pytest.fail(f"黄金输出夹具不存在: {_GOLDEN_FIXTURE_PATH}")
    return json.loads(_GOLDEN_FIXTURE_PATH.read_text(encoding="utf-8"))


_RECORDED: dict[str, Any] = {}


def _write_fixture() -> None:
    existing: dict[str, Any] = {}
    if _GOLDEN_FIXTURE_PATH.exists():
        try:
            existing = json.loads(_GOLDEN_FIXTURE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing.update(_RECORDED)
    text = json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _GOLDEN_FIXTURE_PATH.write_bytes(text.encode("utf-8"))


def _diff(expected: Any, actual: Any, path: str) -> str | None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            return f"{path}: 缺少子键 {missing}"
        if extra:
            return f"{path}: 多出子键 {extra}"
        for key in expected:
            difference = _diff(expected[key], actual[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{path}: 长度不同 expected={len(expected)} actual={len(actual)}"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            difference = _diff(expected_item, actual_item, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return f"{path}: expected={expected!r} actual={actual!r}"
    return None


def _check(key: str, actual: Any) -> None:
    if _is_regen():
        _RECORDED[key] = actual
        _write_fixture()
        pytest.skip("regenerated")

    expected = _load_fixture()
    if key not in expected:
        pytest.fail(f"黄金输出夹具缺少顶层键: {key}")
    difference = _diff(expected[key], actual, key)
    if difference is not None:
        pytest.fail(f"黄金输出不一致: {difference}")


async def test_golden_retrieve(golden_env: _GoldenEnv) -> None:
    _check("retrieve", await _collect_retrieve(golden_env))


async def test_golden_inject(golden_env: _GoldenEnv) -> None:
    _check("inject", await _collect_inject(golden_env))


async def test_golden_tools(golden_env: _GoldenEnv) -> None:
    _check("tools", await _collect_tools(golden_env))


async def test_golden_consolidate(
    golden_env: _GoldenEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _check("consolidate", await _collect_consolidate(golden_env, monkeypatch))


async def test_golden_schedule(
    golden_env: _GoldenEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _check("schedule", await _collect_schedule(golden_env, monkeypatch))
