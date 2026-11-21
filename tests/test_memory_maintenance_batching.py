"""记忆维护批量化的契约回归测试

这些性质在单实例部署下只是"慢"，在一个进程托管几十个 Bot 实例时会周期性地把整个
事件循环拖住、所有实例一起卡，所以按"会拖垮全部实例"的标准钉死：

1. **分页不能漏** —— 清理会把行移出 `is_inactive=False` 过滤条件，用 OFFSET 分页会因为
   结果集缩短而整页跳过数据。必须是主键游标，且跨页的判定结果要和不分页时逐字相同。
2. **成批写** —— 逐行 `save` / 逐条 Qdrant HTTP 删除在几千条量级上就是几千次往返。
   写库按 ID 分块、删向量按 point 分块，块边界要精确。
3. **让出点** —— 每扫一页、每处理完一个工作区都必须把事件循环还回去，
   否则维护任务会把主对话饿死在后面排队。
4. **转储要有上限** —— 解析失败转储无 TTL、无数量上限、无体积上限就是磁盘泄漏；
   而且必须落进既有的 `space_cleanup` 可清理清单，不能另写一套清理。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.space_cleanup import CleanupRequest, ResourceType
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import maintenance as maintenance_module
from nekro_agent.services.memory import qdrant_manager as qdrant_module
from nekro_agent.services.memory.maintenance import (
    chunked,
    prune_all_workspaces,
    prune_workspace_memories,
)
from nekro_agent.services.memory.qdrant_manager import MemoryQdrantManager
from nekro_agent.services.space_cleanup import cleaner as cleaner_module
from nekro_agent.services.space_cleanup import scanner as scanner_module

MEMORY_MODULES = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_paragraph_person",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_reinforcement_log",
    "nekro_agent.models.db_workspace",
]


@pytest.fixture
async def memory_db():
    """内存 SQLite 上的真实记忆表结构

    分页是数据层的性质，用假存储验证等于把要保护的东西替换掉了，所以这里跑真表。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": MEMORY_MODULES})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def memory_enabled(monkeypatch: pytest.MonkeyPatch):
    """打开记忆总开关并钉住清理阈值，避免测试受部署配置影响"""
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True, raising=False)
    monkeypatch.setattr(config, "MEMORY_PRUNE_PARAGRAPH_THRESHOLD", 0.05, raising=False)
    monkeypatch.setattr(config, "MEMORY_PRUNE_RELATION_THRESHOLD", 0.03, raising=False)
    monkeypatch.setattr(config, "MEMORY_LOG_RETENTION_DAYS", 30, raising=False)


class _RecordingQdrant:
    """记录批量删除调用的 Qdrant 替身"""

    def __init__(self) -> None:
        self.deleted_batches: list[list[int]] = []

    async def delete_paragraphs(self, paragraph_ids: list[int]) -> int:
        self.deleted_batches.append(list(paragraph_ids))
        return len(paragraph_ids)


@pytest.fixture
def recording_qdrant(monkeypatch: pytest.MonkeyPatch) -> _RecordingQdrant:
    recorder = _RecordingQdrant()
    monkeypatch.setattr(maintenance_module, "memory_qdrant_manager", recorder)
    return recorder


async def make_paragraph(workspace_id: int, base_weight: float, content: str = "内容") -> DBMemParagraph:
    """建一条段落；`base_weight=0.0` 必定低于清理阈值，`1.0` 必定高于"""
    return await DBMemParagraph.create(
        workspace_id=workspace_id,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        content=content,
        origin_kind=OriginKind.CONSOLIDATION,
        base_weight=base_weight,
    )


async def make_relation(
    workspace_id: int,
    subject_entity_id: int,
    object_entity_id: int,
    base_weight: float,
    paragraph_id: int | None = None,
) -> DBMemRelation:
    return await DBMemRelation.create(
        workspace_id=workspace_id,
        subject_entity_id=subject_entity_id,
        object_entity_id=object_entity_id,
        predicate="提到",
        memory_source="na",
        cognitive_type="episodic",
        base_weight=base_weight,
        paragraph_id=paragraph_id,
    )


async def make_entity(workspace_id: int, name: str, appearance_count: int = 1) -> DBMemEntity:
    return await DBMemEntity.create(
        workspace_id=workspace_id,
        entity_type=EntityType.CONCEPT,
        name=name,
        canonical_name=name,
        appearance_count=appearance_count,
    )


class TestChunking:
    """写库分块的边界"""

    @pytest.mark.parametrize(
        ("items", "size", "expected"),
        [
            ([], 3, []),
            ([1], 3, [[1]]),
            ([1, 2, 3], 3, [[1, 2, 3]]),
            ([1, 2, 3, 4], 3, [[1, 2, 3], [4]]),
            ([1, 2, 3, 4, 5, 6], 3, [[1, 2, 3], [4, 5, 6]]),
            ([1, 2, 3, 4, 5, 6, 7], 3, [[1, 2, 3], [4, 5, 6], [7]]),
        ],
    )
    def test_chunked_boundaries(self, items: list[int], size: int, expected: list[list[int]]) -> None:
        assert chunked(items, size) == expected


class TestPrunePaging:
    """分页扫描不能漏行，也不能越过工作区边界"""

    async def test_paragraph_pruning_spans_pages_without_skipping(
        self,
        memory_db: None,
        memory_enabled: None,
        recording_qdrant: _RecordingQdrant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 页大小 3、10 行交替低/高权重：如果用 OFFSET 分页，前一页被标记失活后
        # 结果集缩短，第二页起会整页跳过——这里正是要挡住那个形态。
        monkeypatch.setattr(maintenance_module, "PRUNE_SCAN_PAGE_SIZE", 3)

        victim_ids: list[int] = []
        survivor_ids: list[int] = []
        for index in range(10):
            paragraph = await make_paragraph(1, base_weight=0.0 if index % 2 == 0 else 1.0)
            (victim_ids if index % 2 == 0 else survivor_ids).append(paragraph.id)

        result = await prune_workspace_memories(1)

        assert result.paragraphs_pruned == 5
        inactive_ids = await DBMemParagraph.filter(is_inactive=True).order_by("id").values_list("id", flat=True)
        assert list(inactive_ids) == victim_ids
        still_active = await DBMemParagraph.filter(is_inactive=False).order_by("id").values_list("id", flat=True)
        assert list(still_active) == survivor_ids
        assert sorted(pid for batch in recording_qdrant.deleted_batches for pid in batch) == victim_ids

    async def test_protected_and_frozen_paragraphs_survive(
        self,
        memory_db: None,
        memory_enabled: None,
        recording_qdrant: _RecordingQdrant,
    ) -> None:
        plain = await make_paragraph(1, base_weight=0.0)
        protected = await make_paragraph(1, base_weight=0.0)
        protected.is_protected = True
        await protected.save(update_fields=["is_protected"])
        frozen = await make_paragraph(1, base_weight=0.0)
        frozen.is_frozen = True
        await frozen.save(update_fields=["is_frozen"])

        result = await prune_workspace_memories(1)

        assert result.paragraphs_pruned == 1
        assert await DBMemParagraph.filter(is_inactive=True).values_list("id", flat=True) == [plain.id]

    async def test_other_workspace_is_untouched(
        self,
        memory_db: None,
        memory_enabled: None,
        recording_qdrant: _RecordingQdrant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(maintenance_module, "PRUNE_SCAN_PAGE_SIZE", 2)
        mine = [(await make_paragraph(1, base_weight=0.0)).id for _ in range(5)]
        theirs = [(await make_paragraph(2, base_weight=0.0)).id for _ in range(5)]

        await prune_workspace_memories(1)

        assert list(await DBMemParagraph.filter(is_inactive=True).order_by("id").values_list("id", flat=True)) == mine
        assert (
            list(await DBMemParagraph.filter(is_inactive=False).order_by("id").values_list("id", flat=True)) == theirs
        )

    async def test_relations_follow_pruned_paragraphs_across_pages(
        self,
        memory_db: None,
        memory_enabled: None,
        recording_qdrant: _RecordingQdrant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 关系与段落的匹配原本是 O(R×P) 的 list 线性查找，改成集合后结果必须逐字相同：
        # 高权重但来源段落已被清理的关系照样要失活。
        monkeypatch.setattr(maintenance_module, "PRUNE_SCAN_PAGE_SIZE", 2)

        pruned_paragraph = await make_paragraph(1, base_weight=0.0)
        kept_paragraph = await make_paragraph(1, base_weight=1.0)

        orphaned = [(await make_relation(1, 1, 2, base_weight=1.0, paragraph_id=pruned_paragraph.id)).id for _ in range(3)]
        low_weight = [(await make_relation(1, 3, 4, base_weight=0.0, paragraph_id=kept_paragraph.id)).id for _ in range(2)]
        healthy = [(await make_relation(1, 5, 6, base_weight=1.0, paragraph_id=kept_paragraph.id)).id for _ in range(3)]

        result = await prune_workspace_memories(1)

        assert result.relations_pruned == len(orphaned) + len(low_weight)
        inactive = list(await DBMemRelation.filter(is_inactive=True).order_by("id").values_list("id", flat=True))
        assert inactive == sorted(orphaned + low_weight)
        active = list(await DBMemRelation.filter(is_inactive=False).order_by("id").values_list("id", flat=True))
        assert active == healthy

    async def test_only_unreferenced_single_appearance_entities_are_pruned(
        self,
        memory_db: None,
        memory_enabled: None,
        recording_qdrant: _RecordingQdrant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(maintenance_module, "PRUNE_SCAN_PAGE_SIZE", 2)

        referenced = await make_entity(1, "被引用")
        partner = await make_entity(1, "对端")
        frequent = await make_entity(1, "高频", appearance_count=2)
        orphan_a = await make_entity(1, "孤立A")
        orphan_b = await make_entity(1, "孤立B")
        # 这条关系权重高、来源段落也没被清理，所以清理后仍然活跃，两端实体必须留下
        await make_relation(1, referenced.id, partner.id, base_weight=1.0)

        result = await prune_workspace_memories(1)

        assert result.entities_pruned == 2
        inactive = list(await DBMemEntity.filter(is_inactive=True).order_by("id").values_list("id", flat=True))
        assert inactive == sorted([orphan_a.id, orphan_b.id])
        assert await DBMemEntity.filter(id=frequent.id).values_list("is_inactive", flat=True) == [False]


class _AsyncioProbe:
    """代理 `asyncio`，只截住 `sleep` 用于观测让出点"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *args: Any, **kwargs: Any) -> Any:
        self.delays.append(delay)
        return await asyncio.sleep(0, *args, **kwargs)


class TestYieldPoints:
    """长任务必须让出事件循环，否则几十个实例一起被饿死"""

    async def test_prune_yields_once_per_scanned_page_and_paces_workspaces(
        self,
        memory_db: None,
        memory_enabled: None,
        recording_qdrant: _RecordingQdrant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(maintenance_module, "PRUNE_SCAN_PAGE_SIZE", 3)
        for workspace_index in (1, 2):
            await DBWorkspace.create(name=f"ws{workspace_index}")
            for _ in range(7):
                await make_paragraph(workspace_index, base_weight=0.0)

        probe = _AsyncioProbe()
        monkeypatch.setattr(maintenance_module, "asyncio", probe)

        results = await prune_all_workspaces()

        assert sorted(results) == [1, 2]
        # 每个工作区：段落 7 行 / 页 3 = 3 个非空页；关系与实体表为空，首查即空、不产生让出点。
        assert probe.delays.count(0) == 6
        # 工作区之间各留一次限速，避免一口气跑完整个记忆库
        interval = maintenance_module.PRUNE_WORKSPACE_INTERVAL_SECONDS
        assert probe.delays.count(interval) == 2


class _FakeQdrantClient:
    """记录调用的 Qdrant 客户端替身"""

    def __init__(self, existing: list[str] | None = None) -> None:
        self.existing = existing or []
        self.deleted_points: list[list[int]] = []
        self.upserted_batches: list[list[int]] = []
        self.created_collections: list[str] = []
        self.deleted_collections: list[str] = []

    async def get_collections(self) -> Any:
        return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in self.existing])

    async def delete_collection(self, collection_name: str) -> None:
        self.deleted_collections.append(collection_name)

    async def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self.created_collections.append(collection_name)

    async def delete(self, collection_name: str, points_selector: Any) -> None:
        self.deleted_points.append(list(points_selector.points))

    async def upsert(self, collection_name: str, points: list[Any]) -> None:
        self.upserted_batches.append([point.id for point in points])


class TestQdrantBatchDelete:
    """逐条 HTTP 删除改成成批提交，块边界必须精确"""

    @pytest.mark.parametrize(
        ("total", "chunk_size", "expected_batch_sizes"),
        [
            (0, 4, []),
            (1, 4, [1]),
            (4, 4, [4]),
            (5, 4, [4, 1]),
            (8, 4, [4, 4]),
            (9, 4, [4, 4, 1]),
        ],
    )
    async def test_delete_paragraphs_chunks_exactly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        total: int,
        chunk_size: int,
        expected_batch_sizes: list[int],
    ) -> None:
        client = _FakeQdrantClient()
        monkeypatch.setattr(qdrant_module, "DELETE_CHUNK_SIZE", chunk_size)

        async def fake_get_client() -> Any:
            return client

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", fake_get_client)

        paragraph_ids = list(range(1, total + 1))
        deleted = await MemoryQdrantManager("bench").delete_paragraphs(paragraph_ids)

        assert deleted == total
        assert [len(batch) for batch in client.deleted_points] == expected_batch_sizes
        assert [pid for batch in client.deleted_points for pid in batch] == paragraph_ids

    async def test_delete_paragraphs_without_client_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_get_client() -> Any:
            return None

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", fake_get_client)
        assert await MemoryQdrantManager("bench").delete_paragraphs([1, 2, 3]) == 0


class TestRebuildBatching:
    """重建必须分页 + 走 embed_batch，而不是全库进内存 + 串行逐条 embedding"""

    async def test_rebuild_uses_batched_embeddings_and_one_upsert_per_page(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _FakeQdrantClient(existing=["bench"])

        async def fake_get_client() -> Any:
            return client

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", fake_get_client)
        monkeypatch.setattr(qdrant_module, "get_memory_embedding_dimension", lambda: 4)

        embed_calls: list[list[str]] = []

        async def fake_embed_batch(texts: list[str]) -> list[list[float] | None]:
            embed_calls.append(list(texts))
            return [None if text == "无法向量化" else [0.1, 0.2, 0.3, 0.4] for text in texts]

        monkeypatch.setattr(qdrant_module, "embed_batch", fake_embed_batch)

        good_ids = [(await make_paragraph(1, 1.0, content=f"记忆{index}")).id for index in range(5)]
        blank = await make_paragraph(1, 1.0, content="   ")
        failing = await make_paragraph(1, 1.0, content="无法向量化")

        stats = await MemoryQdrantManager("bench").rebuild_collection(batch_size=3)

        assert stats == {"total": 7, "success": 5, "error": 1, "skipped": 1, "dimension": 4}
        # 3 页：前两页各 3 行、末页 1 行；空内容在向量化之前就被剔除
        assert embed_calls == [["记忆0", "记忆1", "记忆2"], ["记忆3", "记忆4"], ["无法向量化"]]
        # 每页一次 upsert，而不是每条一次
        assert client.upserted_batches == [good_ids[:3], good_ids[3:]]
        assert client.deleted_collections == ["bench"]
        assert client.created_collections == ["bench"]

        refs = await DBMemParagraph.filter(id__in=good_ids).order_by("id").values_list("embedding_ref", flat=True)
        assert list(refs) == [str(pid) for pid in good_ids]
        assert await DBMemParagraph.filter(id=blank.id).values_list("embedding_ref", flat=True) == [None]
        assert await DBMemParagraph.filter(id=failing.id).values_list("embedding_ref", flat=True) == [None]


def write_dump(directory: Path, name: str, size: int, age_seconds: float, now_ts: float) -> Path:
    path = directory / name
    path.write_text("x" * size, encoding="utf-8")
    mtime = now_ts - age_seconds
    os.utime(path, (mtime, mtime))
    return path


class TestParseFailureDumpRotation:
    """解析失败转储必须有 TTL、数量上限与体积上限，否则就是一条磁盘泄漏"""

    @pytest.mark.parametrize(
        ("text_length", "max_chars", "expect_truncated"),
        [(10, 10, False), (11, 10, True), (0, 10, False)],
    )
    def test_truncation_boundary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        text_length: int,
        max_chars: int,
        expect_truncated: bool,
    ) -> None:
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_SECTION_CHARS", max_chars)
        text = "a" * text_length
        result = consolidator_module._truncate_dump_section(text)  # noqa: SLF001
        if expect_truncated:
            assert result == "a" * max_chars + f"\n...[truncated {text_length - max_chars} chars]"
        else:
            assert result == text

    def test_expired_dumps_are_removed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_PATH", tmp_path)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_RETENTION_SECONDS", 100)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_FILES", 1000)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_TOTAL_BYTES", 10**9)
        now_ts = 1_700_000_000.0

        write_dump(tmp_path, "expired.log", 10, age_seconds=101, now_ts=now_ts)
        write_dump(tmp_path, "on_boundary.log", 10, age_seconds=100, now_ts=now_ts)
        write_dump(tmp_path, "fresh.log", 10, age_seconds=1, now_ts=now_ts)
        # 非 .log 文件不属于转储，不应被这条清理动到
        (tmp_path / "keep.txt").write_text("x", encoding="utf-8")

        removed = consolidator_module.prune_parse_failure_dumps(now_ts)

        assert removed == 1
        assert sorted(p.name for p in tmp_path.iterdir()) == ["fresh.log", "keep.txt", "on_boundary.log"]

    def test_file_count_cap_drops_oldest_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_PATH", tmp_path)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_RETENTION_SECONDS", 10**9)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_FILES", 3)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_TOTAL_BYTES", 10**9)
        now_ts = 1_700_000_000.0

        for index in range(5):
            write_dump(tmp_path, f"dump{index}.log", 10, age_seconds=100 - index, now_ts=now_ts)

        removed = consolidator_module.prune_parse_failure_dumps(now_ts)

        assert removed == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == ["dump2.log", "dump3.log", "dump4.log"]

    def test_total_byte_cap_drops_oldest_until_under_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_PATH", tmp_path)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_RETENTION_SECONDS", 10**9)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_FILES", 1000)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_TOTAL_BYTES", 250)
        now_ts = 1_700_000_000.0

        for index in range(4):
            write_dump(tmp_path, f"dump{index}.log", 100, age_seconds=100 - index, now_ts=now_ts)

        removed = consolidator_module.prune_parse_failure_dumps(now_ts)

        assert removed == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == ["dump2.log", "dump3.log"]

    async def test_parse_failure_dump_is_capped_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_PATH", tmp_path)
        monkeypatch.setattr(consolidator_module, "MEMORY_PARSE_LOG_MAX_FILES", 2)
        consolidator = consolidator_module.EpisodicConsolidator(workspace_id=1)

        for _ in range(5):
            with pytest.raises(consolidator_module.MemoryResponseParseError):
                await consolidator._parse_memory_response("这不是 JSON {")  # noqa: SLF001

        assert len(list(tmp_path.glob("*.log"))) == 2


class TestSpaceCleanupRegistration:
    """转储目录必须接进既有的 space_cleanup 机制"""

    @pytest.fixture
    def cleanup_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        data_dir = tmp_path / "data"
        dump_dir = data_dir / "logs" / "app" / "memory_parse_failures"
        dump_dir.mkdir(parents=True)
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        monkeypatch.setattr(cleaner_module, "MEMORY_PARSE_LOG_DIR", str(dump_dir))
        return dump_dir

    async def test_collect_includes_memory_parse_dumps(self, cleanup_dirs: Path) -> None:
        first = cleanup_dirs / "a.log"
        first.write_text("a", encoding="utf-8")
        second = cleanup_dirs / "b.log"
        second.write_text("b", encoding="utf-8")

        files = await cleaner_module.cleanup_service._collect_files_to_clean(  # noqa: SLF001
            CleanupRequest(
                resource_types=[ResourceType.MEMORY_PARSE_LOGS],
                chat_keys=None,
                before_date=None,
                dry_run=True,
            ),
        )

        assert sorted(path.name for path in files) == ["a.log", "b.log"]

    async def test_before_date_filter_applies_to_memory_parse_dumps(self, cleanup_dirs: Path) -> None:
        old_file = cleanup_dirs / "old.log"
        old_file.write_text("old", encoding="utf-8")
        old_mtime = (datetime.now() - timedelta(days=30)).timestamp()
        os.utime(old_file, (old_mtime, old_mtime))
        new_file = cleanup_dirs / "new.log"
        new_file.write_text("new", encoding="utf-8")

        files = await cleaner_module.cleanup_service._collect_files_to_clean(  # noqa: SLF001
            CleanupRequest(
                resource_types=[ResourceType.MEMORY_PARSE_LOGS],
                chat_keys=None,
                before_date=datetime.now(timezone.utc) - timedelta(days=7),
                dry_run=True,
            ),
        )

        assert [path.name for path in files] == ["old.log"]

    async def test_scanner_reports_dumps_without_double_counting_app_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app_log_dir = tmp_path / "logs" / "app"
        dump_dir = app_log_dir / "memory_parse_failures"
        dump_dir.mkdir(parents=True)
        (app_log_dir / "app.log").write_text("1234567890", encoding="utf-8")
        (dump_dir / "a.log").write_text("12345", encoding="utf-8")
        (dump_dir / "b.log").write_text("12345", encoding="utf-8")

        monkeypatch.setattr(scanner_module, "APP_LOG_DIR", str(app_log_dir))
        monkeypatch.setattr(scanner_module, "MEMORY_PARSE_LOG_DIR", str(dump_dir))
        service = scanner_module.ScannerService()

        dumps = await service._scan_memory_parse_logs()  # noqa: SLF001
        app_logs = await service._scan_app_logs()  # noqa: SLF001

        assert (dumps.file_count, dumps.total_size) == (2, 10)
        assert dumps.can_cleanup is True
        # 转储单列成分类后，应用日志分类不能再把同一批文件算第二遍
        assert (app_logs.file_count, app_logs.total_size) == (1, 10)
