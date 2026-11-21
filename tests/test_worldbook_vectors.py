"""世界书语义触发：向量同步、检索、与关键词命中走同一套后处理。

全部跑在 SQLite 内存库与本地 Qdrant 内存实例上；嵌入用确定性的 8 维假向量，不碰外网。
这些用例钉住的是「默认路径一次嵌入请求都不发」与「语义命中只是换了一个 HitReason」两件事：
延迟 / 冷却 / 粘性、按用途路由、模板、预算、条数上限、信任级别都由既有的同一套代码处理。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import worldbook as worldbook_router
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.worldbook import EntryUpdateRequest, TriggerTestRequest
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory.prompt_rounds import _WorldbookItemDraft
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import matcher, runtime, st_format, timed_state, tokens, vectors
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook import unsupported as wb_unsupported
from nekro_agent.services.worldbook.budget import resolve_budget
from nekro_agent.services.worldbook.types import WindowMessage

ADAPTER = "onebot_v11"
INSTANCE = "v1a"
OTHER_INSTANCE = "v1b"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_1"
DIMENSION = 8
# 词到维度的固定映射：文本里出现哪个字，对应维度就加一，归一化后余弦相似度可预测。
TOPIC_DIMENSIONS = {"猫": 0, "狗": 1, "雨": 2, "晴": 3, "剑": 4, "盾": 5, "火": 6, "水": 7}


def _fake_vector(text: str) -> list[float]:
    values = [0.01] * DIMENSION
    for word, index in TOPIC_DIMENSIONS.items():
        if word in text:
            values[index] += 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


@dataclass
class Env:
    client: AsyncQdrantClient
    channel: DBChatChannel
    embed_calls: list[str]


@pytest.fixture(autouse=True)
def _reset_module_state() -> AsyncIterator[None]:
    vectors._ready_signature = None  # noqa: SLF001
    vectors._query_cache.clear()  # noqa: SLF001
    vectors._error_log_at.clear()  # noqa: SLF001
    runtime._rounds.clear()  # noqa: SLF001
    timed_state.timed_state_store.clear()
    yield
    vectors._ready_signature = None  # noqa: SLF001
    vectors._query_cache.clear()  # noqa: SLF001
    vectors._error_log_at.clear()  # noqa: SLF001
    runtime._rounds.clear()  # noqa: SLF001
    timed_state.timed_state_store.clear()


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)
    monkeypatch.setattr(vectors, "get_memory_embedding_dimension", lambda: DIMENSION)
    monkeypatch.setattr(vectors, "get_memory_embedding_model_group", lambda: "test-group")

    calls: list[str] = []

    async def fake_embed(text: str, **_kwargs: Any) -> list[float]:
        calls.append(text)
        return _fake_vector(text)

    async def fake_batch(texts: list[str], **_kwargs: Any) -> list[list[float] | None]:
        calls.extend(texts)
        return [_fake_vector(text) for text in texts]

    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_batch)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(vectors, "get_qdrant_client", fake_get_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await tokens.ensure_loaded_async()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name="实例一",
        tenant_id=None,
        provider_account_id="bot-v1a",
    )
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=OTHER_INSTANCE,
        display_name="实例二",
        tenant_id=None,
        provider_account_id="bot-v1b",
    )
    channel = await DBChatChannel.create(
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_1",
        channel_name="频道一",
        channel_type="group",
        preset_id=0,
        data="{}",
        workspace_id=None,
    )
    try:
        yield Env(client=client, channel=channel, embed_calls=calls)
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _book(
    env: Env,
    *,
    name: str = "书一",
    instance_key: str = INSTANCE,
    vector_all: bool = False,
    vector_query_messages: int = 2,
    vector_max_entries: int = 5,
    vector_threshold: float = 0.25,
    inject_mode: str = "push",
) -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        description="",
        scope_kind="domain",
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=0,
        enabled=True,
        scan_depth=4,
        budget_tokens=0,
        inject_mode=inject_mode,
        active_scene="both",
        vector_all=vector_all,
        vector_query_messages=vector_query_messages,
        vector_max_entries=vector_max_entries,
        vector_threshold=vector_threshold,
    )


async def _entry(
    book: DBWbBook,
    *,
    uid: int,
    content: str = "正文",
    vectorized: bool = False,
    constant: bool = False,
    keys: tuple[str, ...] = (),
    enabled: bool = True,
    status: str = "active",
    sticky: int | None = None,
    cooldown: int | None = None,
    order: int = 100,
) -> DBWbEntry:
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        comment="",
        keys=list(keys),
        secondary_keys=[],
        selective_logic="and_any",
        use_regex=False,
        constant=constant,
        content=content,
        order=order,
        position="before_history",
        depth=4,
        sticky=sticky,
        cooldown=cooldown,
        enabled=enabled,
        trusted=False,
        vectorized=vectorized,
        status=status,
        ext={},
    )


def _window(*texts: str) -> list[WindowMessage]:
    return [
        WindowMessage(id=index + 1, sender_id="u1", platform_userid="u1", content_text=text)
        for index, text in enumerate(texts)
    ]


async def _evaluate(env: Env, window: list[WindowMessage], **kwargs: Any) -> runtime.Evaluation:
    budget = resolve_budget(
        SimpleNamespace(WORLDBOOK_BUDGET_PERCENT=100, WORLDBOOK_BUDGET_CAP_TOKENS=0),
    )
    return await runtime.evaluate(
        env.channel,
        preset_id=0,
        window=window,
        budget=budget,
        max_entries=20,
        use_timed_state=False,
        suppress_untrusted=False,
        **kwargs,
    )


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


class TestDefaultPath:
    async def test_no_participating_entry_sends_no_embedding(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", keys=("猫",))

        result = await _evaluate(env, _window("猫"))
        without_semantic = await _evaluate(env, _window("猫"), semantic=False)

        assert env.embed_calls == []
        assert _ids(result) == _ids(without_semantic) == {entry.id}

    async def test_disabled_vector_switch_still_sends_nothing(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        assert await vectors.sync_pending() == 1
        env.embed_calls.clear()

        channel_config = SimpleNamespace(
            WORLDBOOK_ENABLED=True,
            WORLDBOOK_VECTOR_ENABLED=False,
            WORLDBOOK_BUDGET_PERCENT=100,
            WORLDBOOK_BUDGET_CAP_TOKENS=0,
            AI_CHAT_CONTEXT_EXPIRE_SECONDS=10**9,
        )
        result = await runtime.resolve_round("code-vector-off", env.channel, channel_config)

        assert env.embed_calls == []
        assert result.state == "none"
        assert entry.id not in _round_ids(result)


def _ids(result: runtime.Evaluation) -> set[int]:
    return {hit.entry.id for hit in result.chosen.selected}


def _round_ids(result: runtime.RoundWorldbook) -> set[int]:
    groups = result.groups
    ids = {hit.entry.id for hit in groups.after_persona}
    ids |= {hit.entry.id for hit in groups.before_history}
    for depth_hits in groups.at_depth.values():
        ids |= {hit.entry.id for hit in depth_hits}
    return ids


async def _sync_and_refresh(*entries: DBWbEntry) -> int:
    count = await vectors.sync_pending()
    for entry in entries:
        await entry.refresh_from_db()
    return count


class TestSemanticHits:
    async def test_semantic_hit_has_kind_and_score(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        assert await vectors.sync_pending() == 1
        env.embed_calls.clear()

        result = await _evaluate(env, _window("猫"))

        assert entry.id in _ids(result)
        hit = next(hit for hit in result.hits if hit.entry.id == entry.id)
        assert hit.reason.kind == "semantic"
        assert hit.reason.score is not None and hit.reason.score > 0.25

    async def test_below_threshold_is_not_a_hit(self, env: Env) -> None:
        book = await _book(env, vector_threshold=0.99)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await vectors.sync_pending()
        env.embed_calls.clear()

        result = await _evaluate(env, _window("狗"))

        assert entry.id not in _ids(result)
        assert env.embed_calls  # 有候选，所以确实算了一次查询向量

    async def test_max_entries_limits_hits_per_book(self, env: Env) -> None:
        book = await _book(env, vector_max_entries=1, vector_threshold=0.0)
        entries = [await _entry(book, uid=uid, content="猫的习性", vectorized=True) for uid in range(3)]
        await _sync_and_refresh(*entries)

        scores = await vectors.semantic_hits([book], entries, _window("猫"), requester=None)

        assert len(scores) == 1

    async def test_keyword_hit_takes_priority(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", keys=("猫",), vectorized=True)
        await vectors.sync_pending()

        result = await _evaluate(env, _window("猫"))

        hit = next(hit for hit in result.hits if hit.entry.id == entry.id)
        assert hit.reason.kind == "key"

    async def test_vector_all_covers_non_constant_entries_only(self, env: Env) -> None:
        book = await _book(env, vector_all=True)
        constant = await _entry(book, uid=0, content="猫的常驻设定", constant=True)
        normal = await _entry(book, uid=1, content="猫的普通设定")
        disabled = await _entry(book, uid=2, content="猫的停用设定", enabled=False)
        await _sync_and_refresh(constant, normal, disabled)

        assert constant.vector_hash == ""
        assert disabled.vector_hash == ""
        assert normal.vector_hash != ""
        result = await _evaluate(env, _window("猫"))
        reasons = {hit.entry.id: hit.reason.kind for hit in result.hits}
        assert reasons[normal.id] == "semantic"
        # 常驻条目照旧由 `constant` 命中，而不是被语义再带出来一次。
        assert reasons[constant.id] == "constant"
        assert disabled.id not in _ids(result)

    async def test_unsynced_entry_only_matches_after_sync(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)

        before = await _evaluate(env, _window("猫"))
        assert entry.id not in _ids(before)

        assert await vectors.sync_pending() == 1
        after = await _evaluate(env, _window("猫"))
        assert entry.id in _ids(after)

    async def test_content_edit_clears_hash_and_reembeds(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await _sync_and_refresh(entry)
        old_hash = entry.vector_hash
        assert old_hash

        await wb_service.update_entry(
            entry,
            EntryUpdateRequest(content="狗的习性"),
            is_platform=True,
            username="tester",
        )
        await entry.refresh_from_db()
        assert entry.vector_hash == ""

        await _sync_and_refresh(entry)
        old_topic = await vectors.semantic_hits([book], [entry], _window("猫"), requester=None)
        new_topic = await vectors.semantic_hits([book], [entry], _window("狗"), requester=None)

        assert entry.id not in old_topic
        assert entry.id in new_topic

    async def test_cooldown_and_sticky_apply_to_semantic_hits(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True, sticky=2, cooldown=2)
        await vectors.sync_pending()
        match = runtime.entry_to_match(entry, runtime.book_to_match(book))
        semantic = matcher.HitReason(kind="semantic", score=0.9)

        sticky_hit, _wants = timed_state.apply_timed(match, semantic, timed_state.TimedGate.STICKY, 10)
        cooldown_hit, _wants = timed_state.apply_timed(match, semantic, timed_state.TimedGate.COOLDOWN, 10)

        assert sticky_hit is not None and sticky_hit.kind == "sticky"
        assert cooldown_hit is None

    async def test_lookup_book_is_not_auto_injected(self, env: Env) -> None:
        book = await _book(env, inject_mode="lookup")
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await vectors.sync_pending()
        env.embed_calls.clear()

        result = await _evaluate(env, _window("猫"))

        assert entry.id not in _ids(result)
        assert env.embed_calls == []

    async def test_embedding_error_keeps_round_alive(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await vectors.sync_pending()
        vectors._query_cache.clear()  # noqa: SLF001

        async def failing_embed(_text: str, **_kwargs: Any) -> list[float]:
            raise RuntimeError("上游不可用")

        monkeypatch.setattr(embedding_service_module, "embed_text", failing_embed)
        result = await _evaluate(env, _window("猫"))

        assert entry.id not in _ids(result)

    async def test_timeout_keeps_round_alive(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book(env)
        await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await vectors.sync_pending()
        vectors._query_cache.clear()  # noqa: SLF001
        monkeypatch.setattr(vectors, "SEMANTIC_TIMEOUT_SECONDS", 0.01)

        async def slow_embed(text: str, **_kwargs: Any) -> list[float]:
            await asyncio.sleep(0.1)
            return _fake_vector(text)

        monkeypatch.setattr(embedding_service_module, "embed_text", slow_embed)

        result = await _evaluate(env, _window("猫"))

        assert _ids(result) == set()

    async def test_other_instance_and_orphan_points_are_ignored(self, env: Env) -> None:
        own_book = await _book(env, name="自己的书")
        own_entry = await _entry(own_book, uid=0, content="猫的习性", vectorized=True)
        other_book = await _book(env, name="别的书", instance_key=OTHER_INSTANCE)
        other_entry = await _entry(other_book, uid=0, content="猫的习性", vectorized=True)
        await _sync_and_refresh(own_entry, other_entry)
        await env.client.upsert(
            collection_name=vectors.COLLECTION,
            points=[
                qdrant_models.PointStruct(
                    id=999_999,
                    vector=_fake_vector("猫"),
                    payload={"entry_id": 999_999, "book_id": own_book.id, "content_hash": "x"},
                ),
            ],
        )

        # 调用方传进来的书单就是这一轮生效的书：别的实例的书不在里面，它的条目也不会算候选。
        scores = await vectors.semantic_hits(
            [own_book],
            [own_entry, other_entry],
            _window("猫"),
            requester=None,
        )

        assert set(scores) == {own_entry.id}
        result = await _evaluate(env, _window("猫"))
        assert _ids(result) == {own_entry.id}


class TestLifecycle:
    async def test_delete_entries_removes_points_and_swallows_errors(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await vectors.sync_pending()
        assert await env.client.retrieve(collection_name=vectors.COLLECTION, ids=[entry.id])

        await vectors.delete_entries([entry.id])
        assert not await env.client.retrieve(collection_name=vectors.COLLECTION, ids=[entry.id])

        async def failing_delete(**_kwargs: Any) -> None:
            raise RuntimeError("Qdrant 不可用")

        monkeypatch.setattr(env.client, "delete", failing_delete)
        await vectors.delete_entries([entry.id])
        await vectors.delete_books([book.id])

    async def test_st_round_trip_and_backfill(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = {
            "uid": 3,
            "comment": "标题",
            "key": [],
            "content": "猫的习性",
            "vectorized": True,
        }
        columns, _notes = st_format.st_entry_to_columns(raw)
        assert columns["vectorized"] is True
        restored = st_format.columns_to_st_entry(columns, raw)
        assert restored["vectorized"] is True
        assert "vectorized" not in {feature.key for feature in wb_unsupported.FEATURES}

        book = await _book(env)
        legacy = await _entry(book, uid=1, content="旧条目")
        legacy.ext = {"vectorized": True}
        await legacy.save(update_fields=["ext"])
        monkeypatch.setattr(config, "WORLDBOOK_VECTOR_BACKFILLED", False)

        assert await vectors.backfill_legacy_vectorized() == 1
        await legacy.refresh_from_db()
        assert legacy.vectorized is True
        assert await vectors.backfill_legacy_vectorized() == 0

    async def test_book_counts(self, env: Env) -> None:
        book = await _book(env, vector_all=True)
        await _entry(book, uid=0, content="常驻", constant=True, vectorized=True)
        await _entry(book, uid=1, content="普通")
        await _entry(book, uid=2, content="停用", enabled=False)

        participating, pending = await vectors.participation_counts(book)

        assert participating == 1
        assert pending == 1


class TestSurfaces:
    async def test_trigger_test_returns_semantic_score(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)
        await vectors.sync_pending()
        env.embed_calls.clear()

        response = await worldbook_router.test_trigger(
            TriggerTestRequest(chat_key=CHAT_KEY, text="猫"),
            _current_user=_platform_user(),
        )

        hit = next(item for item in response.hits if item.entry_id == entry.id)
        assert hit.reason_kind == "semantic"
        assert hit.score is not None and hit.score > 0.25

    async def test_prompt_detail_keeps_reason_and_old_rows(self, env: Env) -> None:
        book = await _book(env)
        entry = await _entry(book, uid=0, content="猫的习性", vectorized=True)

        items = await wb_service.describe_round_items(
            [
                {"id": entry.id, "reason": "semantic", "score": 0.42},
                {"id": entry.id},
            ],
        )

        assert items[0]["reason"] == "semantic"
        assert items[0]["score"] == 0.42
        assert "reason" not in items[1]
        draft = _WorldbookItemDraft(entry_id=entry.id, book_id=book.id, chars=4, position="before_history", trusted=False)
        assert "reason" not in draft.as_dict()
