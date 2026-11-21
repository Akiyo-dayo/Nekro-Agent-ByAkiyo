"""隐私矩阵与 151 用例共用的检索沙盘。

只放布置与断言工具，不放用例：同一份真实检索出口（`retrieve_memories` 与
`memory_tools`）要供两个测试文件复用，各写一份布置代码迟早会分叉。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlencode

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

import plugins.builtin.memory_tools.main as memory_tools_module
from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import resolve_memory_scope
from nekro_agent.services.memory.persons import reset_member_cache
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.retriever import retrieve_memories
from nekro_agent.services.memory.scope_condition import MemTable, evaluate

ADAPTER = "onebot_v11"
I1 = "i1"
I2 = "i2"
I3 = "i3"
BOT_ACCOUNT = "ubot"

U151 = "u151"
U151B = "u151b"
UXW = "uxw"
UXH = "uxh"

VECTOR = [0.1, 0.2, 0.3, 0.4]
SEARCH_LIMIT = 50

PG_DSN_ENV = "NA_TEST_PG_DSN"


def pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(f"需要真实 PostgreSQL：设 {PG_DSN_ENV} 后运行；SQLite 腿始终运行。")
    return dsn


@dataclass(frozen=True)
class SwitchState:
    group_to_group: str = "off"
    group_to_private: str = "off"
    private_to_group: str = "off"

    def as_dict(self) -> dict[str, str]:
        return {
            "MEMORY_RECALL_GROUP_TO_GROUP": self.group_to_group,
            "MEMORY_RECALL_GROUP_TO_PRIVATE": self.group_to_private,
            "MEMORY_RECALL_PRIVATE_TO_GROUP": self.private_to_group,
        }


@dataclass
class ParagraphSeed:
    paragraph_id: int
    content: str
    instance_key: str
    origin_chat_key: Optional[str]
    origin_chat_type: str
    persons: tuple[str, ...]
    workspace_id: int = 0
    preset_id: int = 0

    def as_row(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "adapter_key": ADAPTER,
            "instance_key": self.instance_key,
            "preset_id": self.preset_id,
            "origin_chat_key": self.origin_chat_key,
            "origin_chat_type": self.origin_chat_type,
            "person_accounts": list(self.persons),
        }


class Harness:
    """一个内存 / 真 PG 库 + 一个本地 Qdrant collection + 一份开关状态。"""

    def __init__(self, *, client: AsyncQdrantClient) -> None:
        self.client = client
        self.global_state = SwitchState()
        self.instance_overrides: dict[str, SwitchState] = {}
        self.channel_overrides: dict[str, SwitchState] = {}
        self.paragraphs: list[ParagraphSeed] = []

    def effective_state(self, chat_key: str, instance_key: str) -> SwitchState:
        if chat_key in self.channel_overrides:
            return self.channel_overrides[chat_key]
        if instance_key in self.instance_overrides:
            return self.instance_overrides[instance_key]
        return self.global_state

    def effective_config(self, chat_key: str, instance_key: str) -> SimpleNamespace:
        state = self.effective_state(chat_key, instance_key)
        return SimpleNamespace(
            MEMORY_ENABLE_FOR_CHAT=True,
            MEMORY_MEMBER_ACTIVE_DAYS=config.MEMORY_MEMBER_ACTIVE_DAYS,
            **state.as_dict(),
        )

    def set_global(self, state: SwitchState) -> None:
        self.global_state = state

    def set_instance(self, instance_key: str, state: SwitchState) -> None:
        self.instance_overrides[instance_key] = state

    def set_channel(self, chat_key: str, state: SwitchState) -> None:
        self.channel_overrides[chat_key] = state

    def reset_overrides(self) -> None:
        self.instance_overrides.clear()
        self.channel_overrides.clear()

    async def create_channel(
        self,
        chat_key: str,
        *,
        instance_key: str = I1,
        channel_type: str = "group",
    ) -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key,
            channel_name=chat_key,
            channel_type=channel_type,
            chat_key=chat_key,
            # 不绑工作区：这样检索只走域腿，跨频道开关的判据不会被工作区腿掩盖。
            workspace_id=None,
            data="{}",
        )

    async def create_message(
        self,
        chat_key: str,
        *,
        account: str,
        seconds_ago: int,
        index: int,
        sender_id: Optional[str] = None,
        instance_key: str = I1,
        chat_type: str = "group",
    ) -> DBChatMessage:
        return await DBChatMessage.create(
            sender_id=sender_id if sender_id is not None else account,
            sender_name=account or "空账号",
            sender_nickname=account or "空账号",
            is_tome=0,
            is_recalled=False,
            adapter_key=ADAPTER,
            instance_key=instance_key,
            message_id=f"{chat_key}-m{index}",
            chat_key=chat_key,
            chat_type=chat_type,
            platform_userid=account,
            content_text=f"消息 {index}",
            content_data="[]",
            raw_cq_code="",
            ext_data="{}",
            send_timestamp=int(time.time()) - seconds_ago,
        )

    async def create_paragraph(
        self,
        *,
        content: str,
        origin_chat_key: Optional[str],
        origin_chat_type: str,
        persons: tuple[str, ...],
        instance_key: str = I1,
        anchor_msg_id: Optional[str] = None,
        anchor_msg_id_start: Optional[str] = None,
        anchor_msg_id_end: Optional[str] = None,
    ) -> ParagraphSeed:
        paragraph = await DBMemParagraph.create(
            workspace_id=0,
            adapter_key=ADAPTER,
            instance_key=instance_key,
            preset_id=0,
            origin_chat_key=origin_chat_key,
            origin_chat_type=origin_chat_type,
            memory_source="na",
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=KnowledgeType.FACT,
            content=content,
            summary=content,
            origin_kind=OriginKind.CONSOLIDATION,
            anchor_msg_id=anchor_msg_id,
            anchor_msg_id_start=anchor_msg_id_start,
            anchor_msg_id_end=anchor_msg_id_end,
            event_time=None,
        )
        for account in persons:
            await DBMemParagraphPerson.create(paragraph_id=paragraph.id, account=account)
        seed = ParagraphSeed(
            paragraph_id=paragraph.id,
            content=content,
            instance_key=instance_key,
            origin_chat_key=origin_chat_key,
            origin_chat_type=origin_chat_type,
            persons=tuple(persons),
        )
        self.paragraphs.append(seed)
        await self.client.upsert(
            collection_name=memory_qdrant_manager.collection_name,
            points=[
                qdrant_models.PointStruct(
                    id=paragraph.id,
                    vector=list(VECTOR),
                    payload=paragraph.to_qdrant_payload(person_accounts=list(persons)),
                ),
            ],
        )
        return seed

    async def expected_ids(self, channel: DBChatChannel) -> set[int]:
        scope = await resolve_memory_scope(channel)
        condition = scope.condition(MemTable.PARAGRAPH, workspace_id=channel.workspace_id or 0)
        return {seed.paragraph_id for seed in self.paragraphs if evaluate(condition, seed.as_row())}

    async def retrieve(self, channel: DBChatChannel, query: str = "麦当劳") -> list[Any]:
        scope = await resolve_memory_scope(channel)
        return await retrieve_memories(
            channel.workspace_id or 0,
            query,
            limit=SEARCH_LIMIT,
            scope=scope,
            requester=(channel.adapter_key, channel.instance_key),
        )

    async def retrieve_ids(self, channel: DBChatChannel, query: str = "麦当劳") -> set[int]:
        return {int(memory.target_id) for memory in await self.retrieve(channel, query)}

    async def tool_search(self, channel: DBChatChannel, query: str = "麦当劳") -> list[dict[str, Any]]:
        resource = await memory_tools_module.search_workspace_memories_tool(
            _ToolContext(channel),
            query,
            limit=SEARCH_LIMIT,
        )
        return list(json.loads(resource.body)["items"])

    async def tool_search_ids(self, channel: DBChatChannel, query: str = "麦当劳") -> set[int]:
        return {int(item["target_id"]) for item in await self.tool_search(channel, query)}

    async def tool_trace(self, channel: DBChatChannel, memory_id: int) -> dict[str, Any]:
        resource = await memory_tools_module.trace_memory_origin_tool(
            _ToolContext(channel),
            "paragraph",
            memory_id,
        )
        return dict(json.loads(resource.body))

    async def tool_detail(self, channel: DBChatChannel, memory_id: int) -> dict[str, Any]:
        resource = await memory_tools_module.get_memory_detail_tool(
            _ToolContext(channel),
            "paragraph",
            memory_id,
        )
        return dict(json.loads(resource.body))


class _ToolContext:
    """`memory_tools` 只需要这两个属性；作用域一律由服务端从频道推导。"""

    def __init__(self, channel: DBChatChannel) -> None:
        self.db_chat_channel = channel
        self.from_chat_key = channel.chat_key

    async def get_bound_workspace(self) -> None:
        return None


@dataclass
class MemoryEnv:
    harness: Harness
    backend: str


async def _init_sqlite() -> None:
    EXECUTOR_CACHE.clear()
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()


async def _init_pg(dsn: str) -> tuple[Any, str]:
    import asyncpg

    schema = f"na_test_privacy_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": ["nekro_agent.models"]},
    )
    await Tortoise.generate_schemas()
    rows = await connections.get("default").execute_query_dict(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = 'mem_paragraph'",
    )
    assert schema in {str(row["table_schema"]) for row in rows}, "没有在真 PG 的目标 schema 里建出表"
    return admin, schema


@pytest.fixture(params=["sqlite", "pg"], ids=["sqlite", "pg"])
async def memory_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """真表 + 本地 Qdrant + 确定性假嵌入；PG 跑随机 schema，结束 DROP。"""
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_MEMBER_ACTIVE_DAYS", 30)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return list(VECTOR)

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [list(VECTOR) for _ in texts]

    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", fake_embed_batch)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)

    harness = Harness(client=client)

    async def fake_effective_config(self: DBChatChannel) -> SimpleNamespace:
        return harness.effective_config(self.chat_key, self.instance_key)

    async def fake_preset_id(self: DBChatChannel) -> int:
        return 0

    monkeypatch.setattr(DBChatChannel, "get_effective_config", fake_effective_config)
    monkeypatch.setattr(DBChatChannel, "get_effective_preset_id", fake_preset_id)

    reset_member_cache()
    admin: Any = None
    schema = ""
    if request.param == "pg":
        admin, schema = await _init_pg(pg_dsn_or_skip())
    else:
        await _init_sqlite()

    for instance_key, tenant_id in ((I1, 1), (I2, 1), (I3, 2)):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            provider_account_id=BOT_ACCOUNT,
            tenant_id=tenant_id,
        )

    await memory_qdrant_manager.ensure_collection(len(VECTOR))
    try:
        yield MemoryEnv(harness=harness, backend=request.param)
    finally:
        reset_member_cache()
        await client.close()
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        if admin is not None:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()


__all__ = [
    "ADAPTER",
    "BOT_ACCOUNT",
    "I1",
    "I2",
    "I3",
    "MemoryEnv",
    "ParagraphSeed",
    "SEARCH_LIMIT",
    "SwitchState",
    "U151",
    "U151B",
    "UXH",
    "UXW",
    "Harness",
    "memory_env",
]
