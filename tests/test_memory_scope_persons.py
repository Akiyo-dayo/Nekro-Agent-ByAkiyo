"""条件树的人这一维：化简、渲染器与 Qdrant 数组语义。

人这一维只挂在段落上：`PersonsAny(S)` 表示「至少一个涉及的人在 S 里」，
`PersonsOutside(S)` 表示「至少一个不在」，二者合起来表达「非空且全在 S 里」。
本文件把这三种语义分别钉死在纯 Python、SQLite、真 PG 与本地 Qdrant 上。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models

# 复用固定表结构：同一份工具函数只维护一处。
from test_memory_scope_condition import (
    _VECTOR,
    MemoryArena,
    RowSpec,
    _new_scope,
)
from test_memory_scope_condition import arena as _shared_arena_fixture
from test_memory_scope_condition import db as _shared_db_fixture

from nekro_agent.core.config import config
from nekro_agent.services.memory.scope_condition import (
    FALSE,
    MemTable,
    PersonsAny,
    PersonsOutside,
    ScopeField,
    and_,
    eq,
    evaluate,
    not_,
    persons_all_in,
    persons_any,
    to_qdrant_filter,
    to_tortoise_q,
)

db = _shared_db_fixture
arena = _shared_arena_fixture

_VECTOR_SIZE = len(_VECTOR)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)


class TestConstructors:
    def test_empty_sets_simplify_to_false(self) -> None:
        assert persons_any([]) is FALSE
        assert persons_all_in([]) is FALSE
        assert persons_any(iter(())) is FALSE
        assert persons_all_in(iter(())) is FALSE

    def test_accounts_are_deduplicated_and_sorted(self) -> None:
        assert persons_any(["b", "a", "b"]) == PersonsAny(accounts=("a", "b"))
        assert persons_all_in(["b", "a", "b"]) == and_(
            PersonsAny(accounts=("a", "b")),
            not_(PersonsOutside(accounts=("a", "b"))),
        )

    def test_not_only_accepts_persons_outside(self) -> None:
        with pytest.raises(ValueError):
            not_(eq(ScopeField.WORKSPACE_ID, 0))
        with pytest.raises(ValueError):
            not_(persons_any(["a"]))
        with pytest.raises(ValueError):
            not_(and_(PersonsAny(("a",))))
        assert not_(PersonsOutside(("a",))) is not FALSE

    def test_person_nodes_are_rejected_on_other_tables(self) -> None:
        for table in (MemTable.ENTITY, MemTable.RELATION, MemTable.EPISODE):
            with pytest.raises(ValueError):
                to_tortoise_q(persons_any(["u151"]), table)
            with pytest.raises(ValueError):
                to_tortoise_q(persons_all_in(["u151"]), table)

    def test_evaluate_uses_the_person_accounts_column(self) -> None:
        assert evaluate(persons_any(["a"]), {}) is False
        assert evaluate(persons_any(["a"]), {"person_accounts": []}) is False
        assert evaluate(persons_any(["a"]), {"person_accounts": ["b", "a"]}) is True
        assert evaluate(PersonsOutside(("a",)), {"person_accounts": []}) is False
        assert evaluate(PersonsOutside(("a",)), {"person_accounts": ["a"]}) is False
        assert evaluate(PersonsOutside(("a",)), {"person_accounts": ["a", "b"]}) is True
        assert evaluate(persons_all_in(["a"]), {"person_accounts": ["a"]}) is True
        assert evaluate(persons_all_in(["a"]), {"person_accounts": ["a", "b"]}) is False


class TestQdrantArraySemantics:
    async def test_match_any_match_except_and_all_in(self) -> None:
        """数组语义钉子：`[1,2,3]` / `[3]` / `[1,2,4,5]` / `[1,2]`。"""
        client = AsyncQdrantClient(location=":memory:")
        collection = f"persons_semantics_{uuid.uuid4().hex}"
        await client.create_collection(
            collection_name=collection,
            vectors_config=qdrant_models.VectorParams(
                size=_VECTOR_SIZE,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        payloads: list[dict[str, Any]] = [
            {"person_accounts": ["a"]},
            {"person_accounts": ["a", "b"]},
            {"person_accounts": ["b", "c"]},
            {"person_accounts": []},
            {},
        ]
        await client.upsert(
            collection_name=collection,
            points=[
                qdrant_models.PointStruct(id=index, vector=list(_VECTOR), payload=payload)
                for index, payload in enumerate(payloads, 1)
            ],
        )

        async def hit_ids(cond: Any) -> set[int]:
            points = await client.search(
                collection_name=collection,
                query_vector=list(_VECTOR),
                query_filter=to_qdrant_filter(cond),
                limit=10000,
            )
            return {int(point.id) for point in points}

        try:
            assert await hit_ids(persons_any(["a", "b"])) == {1, 2, 3}
            assert await hit_ids(PersonsOutside(("a", "b"))) == {3}
            assert await hit_ids(not_(PersonsOutside(("a", "b")))) == {1, 2, 4, 5}
            assert await hit_ids(persons_all_in(["a", "b"])) == {1, 2}
        finally:
            await client.close()


def _person_spec(
    row_id: int,
    accounts: tuple[str, ...],
    *,
    origin_chat_type: str = "group",
    origin_chat_key: str = "gB",
) -> RowSpec:
    return RowSpec(
        id=row_id,
        workspace_id=0,
        adapter_key="onebot_v11",
        instance_key="ob1",
        preset_id=7,
        origin_chat_key=origin_chat_key,
        origin_chat_type=origin_chat_type,
        person_accounts=accounts,
    )


class TestParagraphOnlyVisibility:
    async def test_switch_branches_are_the_only_extra_visibility(self, arena: MemoryArena) -> None:
        rows = [
            _person_spec(1, ("u151",)),
            _person_spec(2, ("u151", "uxh")),
            _person_spec(3, ()),
            _person_spec(4, ("u151",), origin_chat_type="private", origin_chat_key="p151"),
        ]
        await arena.add(MemTable.PARAGRAPH, rows)
        scope = _new_scope(
            preset_id=7,
            domain_members=[("onebot_v11", "ob1")],
            local_keys=["gA"],
            chat_kind="group",
            group_to_group="related",
            private_to_group="related",
            member_accounts=["u151"],
        )
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=0,
            expected={1, 4},
            label="相关档：全员是成员 / 私聊对象是成员",
        )
