"""域记忆的保留时长与「退避中跳过」的日志

1. 沉淀出的段落一律按情景半衰期（默认 1 天）衰减，不被召回强化的话约四天多就低于自动清理线，
   连「芒果过敏」「生日是哪天」这种关于人的稳定信息也一样。闸开时的域行改成按知识类型给半衰期：
   偏好、事实、决定用语义半衰期，其余不变。
2. 存量由 `extend_stable_half_life` 补齐，清理之前先补；条件写在 UPDATE 里，重复执行不改任何行。
3. 沉淀器还在退避期里时，调度器原来记一条「沉淀失败」的 WARNING；这一轮其实根本没执行。
4. 已经按旧半衰期被自动清理掉的稳定类型段落，按新半衰期还在清理线以上的捞回来（重新嵌入），
   动作记 `retention_restore`；人工删除、隔离过的与其余类型不动。
5. `MEMORY_PRUNE_ENABLED` 每一轮现读，关了马上停、开了马上恢复，不用重启。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.services.memory import admin_service, maintenance
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import retention as retention_module
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory.consolidator import ConsolidationResult
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.retention import (
    AUTO_PRUNE_ACTION,
    MANUAL_PRUNE_ACTION,
    RETENTION_RESTORE_ACTION,
    STABLE_KNOWLEDGE_TYPES,
    consolidation_half_life_seconds,
    extend_stable_half_life,
    restore_done,
    restore_pruned_stable,
    stable_half_life_seconds,
)
from nekro_agent.services.memory.scheduler import ConsolidationPriority, MemoryMaintenanceScheduler
from tests.test_memory_consolidation_quality import (  # noqa: F401 - 夹具按名字注入
    _channel,
    _install_persona,
    _item,
    _message,
    _run,
    memory_env,
)

DAY = 86400
CHAT = "onebot_v11-quality-group_9"


def test_restore_marker_is_isolated_per_test(tmp_path: Path) -> None:
    """下面的捞回用例都靠这一条：标记由 conftest 改道到本用例的临时目录，不落进仓库的数据目录、不串用例"""
    assert retention_module.RESTORE_MARKER_PATH.parent == tmp_path


async def _paragraph(
    *,
    workspace_id: int = 0,
    knowledge_type: KnowledgeType = KnowledgeType.FACT,
    origin_kind: OriginKind = OriginKind.CONSOLIDATION,
    half_life_seconds: int = DAY,
    age_days: float = 0.0,
    is_frozen: bool = False,
) -> DBMemParagraph:
    event_time = datetime.now(timezone.utc) - timedelta(days=age_days)
    return await DBMemParagraph.create(
        workspace_id=workspace_id,
        instance_key="quality",
        adapter_key="onebot_v11",
        origin_chat_key=CHAT,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=knowledge_type,
        content=f"{knowledge_type.value} 段落",
        summary=f"{knowledge_type.value} 段落",
        event_time=event_time,
        half_life_seconds=half_life_seconds,
        origin_kind=origin_kind,
        is_frozen=is_frozen,
    )


@pytest.mark.usefixtures("memory_env")
class TestNewDomainRowsGetHalfLifeByType:
    async def test_stable_types_use_the_semantic_half_life(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_EPISODIC_HALF_LIFE_SECONDS", DAY)
        await _channel(CHAT)
        _install_persona(monkeypatch)
        await _message(CHAT, index=1, account="1234567", nickname="小满", content="我芒果过敏，周末一起去看展")
        kinds = ["preference", "fact", "decision", "conversation", "experience", "emotion"]
        await _run(CHAT, monkeypatch, *[_item(f"小满的{kind}记录", knowledge_type=kind) for kind in kinds])

        rows = {row.knowledge_type.value: row.half_life_seconds for row in await DBMemParagraph.all()}
        assert rows == {
            "preference": 30 * DAY,
            "fact": 30 * DAY,
            "decision": 30 * DAY,
            "conversation": DAY,
            "experience": DAY,
            "emotion": DAY,
        }

    async def test_same_rule_for_workspace_rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        assert consolidation_half_life_seconds(KnowledgeType.FACT) == stable_half_life_seconds()
        # 域行与工作区行同一口径（上游原版落到模型默认的 7200 秒，「1 天」本来就是本仓库定的）
        assert consolidation_half_life_seconds(KnowledgeType.CONVERSATION) == int(config.MEMORY_EPISODIC_HALF_LIFE_SECONDS)
        assert STABLE_KNOWLEDGE_TYPES == {KnowledgeType.PREFERENCE, KnowledgeType.FACT, KnowledgeType.DECISION}


@pytest.mark.usefixtures("memory_env")
class TestExistingRowsAreExtended:
    async def test_only_stable_domain_consolidation_rows_change(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        stable = [await _paragraph(knowledge_type=kind) for kind in STABLE_KNOWLEDGE_TYPES]
        chatter = await _paragraph(knowledge_type=KnowledgeType.CONVERSATION)
        workspace_row = await _paragraph(workspace_id=7)
        milestone = await _paragraph(origin_kind=OriginKind.AFFECTION)
        longer = await _paragraph(half_life_seconds=60 * DAY)

        assert await extend_stable_half_life() == len(stable) + 1, "三条稳定类型的域行加一条工作区行"

        for row in stable:
            assert (await DBMemParagraph.get(id=row.id)).half_life_seconds == 30 * DAY
        assert (await DBMemParagraph.get(id=chatter.id)).half_life_seconds == DAY
        assert (await DBMemParagraph.get(id=workspace_row.id)).half_life_seconds == 30 * DAY, "工作区行也补"
        assert (await DBMemParagraph.get(id=milestone.id)).half_life_seconds == DAY
        assert (await DBMemParagraph.get(id=longer.id)).half_life_seconds == 60 * DAY, "只往长里改"

    async def test_second_run_changes_nothing(
        self,
    ) -> None:
        await _paragraph(knowledge_type=KnowledgeType.PREFERENCE)
        assert await extend_stable_half_life() == 1
        assert await extend_stable_half_life() == 0

    async def test_prune_extends_before_it_judges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """六天前写下、从没被召回过的偏好：按 1 天半衰期早该清掉，补齐之后留下"""
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_PRUNE_PARAGRAPH_THRESHOLD", 0.05)
        preference = await _paragraph(knowledge_type=KnowledgeType.PREFERENCE, age_days=6)
        chatter = await _paragraph(knowledge_type=KnowledgeType.CONVERSATION, age_days=6)

        results = await maintenance.prune_all_workspaces()

        assert results[0].paragraphs_pruned == 1
        assert (await DBMemParagraph.get(id=preference.id)).is_inactive is False
        assert (await DBMemParagraph.get(id=chatter.id)).is_inactive is True, "时效内容照旧按情景半衰期退场"

    async def test_prune_skips_the_domain_partition_when_extending_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preference = await _paragraph(knowledge_type=KnowledgeType.PREFERENCE, age_days=6)

        async def broken() -> int:
            raise RuntimeError("库暂时连不上")

        monkeypatch.setattr(maintenance, "extend_stable_half_life", broken)
        results = await maintenance.prune_all_workspaces()

        assert results == {}, "补不齐就不按旧半衰期清理，工作区与域分区都不清"
        assert (await DBMemParagraph.get(id=preference.id)).is_inactive is False


async def _pruned(
    knowledge_type: KnowledgeType,
    *,
    age_days: float = 6,
    action: str = "prune",
    quarantined: bool = False,
    workspace_id: int = 0,
) -> DBMemParagraph:
    row = await _paragraph(workspace_id=workspace_id, knowledge_type=knowledge_type, age_days=age_days)
    await DBMemParagraph.filter(id=row.id).update(
        is_inactive=True,
        last_manual_action=action,
        last_manual_action_at=datetime.now(timezone.utc),
        quarantined_at=datetime.now(timezone.utc) if quarantined else None,
        quarantine_reason="manual" if quarantined else "",
    )
    return await DBMemParagraph.get(id=row.id)


@pytest.mark.usefixtures("memory_env")
class TestPrunedStableRowsComeBack:
    async def test_only_auto_pruned_stable_rows_come_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_SEMANTIC_HALF_LIFE_DAYS", 30)
        monkeypatch.setattr(config, "MEMORY_PRUNE_PARAGRAPH_THRESHOLD", 0.05)
        back = [await _pruned(kind) for kind in STABLE_KNOWLEDGE_TYPES]
        back.append(await _pruned(KnowledgeType.FACT, workspace_id=7))  # 工作区行同样捞回
        chatter = await _pruned(KnowledgeType.CONVERSATION)
        deleted_by_hand = await _pruned(KnowledgeType.FACT, action="delete")
        quarantined = await _pruned(KnowledgeType.FACT, action="quarantine", quarantined=True)
        pruned_by_hand = await _pruned(KnowledgeType.FACT, action=MANUAL_PRUNE_ACTION)
        too_old = await _pruned(KnowledgeType.PREFERENCE, age_days=200)

        await extend_stable_half_life()
        assert await restore_pruned_stable() == len(back)

        client = await embedding_service_module_client()
        for row in back:
            fresh = await DBMemParagraph.get(id=row.id)
            assert fresh.is_inactive is False
            assert fresh.last_manual_action == RETENTION_RESTORE_ACTION, "后台看得出是被修正捞回的"
            assert fresh.half_life_seconds == 30 * DAY
            points = await client.retrieve(memory_qdrant_manager.collection_name, ids=[row.id])
            assert points, "向量补回去了"
        for row in (chatter, deleted_by_hand, quarantined, pruned_by_hand, too_old):
            fresh = await DBMemParagraph.get(id=row.id)
            assert fresh.is_inactive is True
            assert fresh.last_manual_action == row.last_manual_action, "手动清理的不捞回，不撤销管理员的操作"

        assert restore_done(), "完整做完一遍就记下标记"
        later = await _pruned(KnowledgeType.FACT)
        assert await restore_pruned_stable() == 0, "一次性：之后再被自动清理的，即使还在线上也不再捞"
        assert (await DBMemParagraph.get(id=later.id)).is_inactive is True

    async def test_embedding_failure_leaves_it_for_next_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = await _pruned(KnowledgeType.PREFERENCE)
        working = embedding_service_module.embed_text

        async def broken(_text: str, **_kwargs: object) -> list[float]:
            raise RuntimeError("嵌入服务暂时不可用")

        monkeypatch.setattr(embedding_service_module, "embed_text", broken)
        assert await restore_pruned_stable() == 0
        fresh = await DBMemParagraph.get(id=row.id)
        assert fresh.is_inactive is True and fresh.last_manual_action == AUTO_PRUNE_ACTION
        assert not restore_done(), "有失败就不记标记，下一轮再试"

        monkeypatch.setattr(embedding_service_module, "embed_text", working)
        assert await restore_pruned_stable() == 1, "嵌入恢复后下一轮接着捞"
        assert restore_done()

    async def test_rows_below_the_line_do_not_starve_the_ones_behind(self) -> None:
        for _ in range(5):
            await _pruned(KnowledgeType.FACT, age_days=400)  # 按新半衰期也早在线下
        wanted = await _pruned(KnowledgeType.PREFERENCE)

        assert await restore_pruned_stable(limit=1, page_size=2) == 1
        assert (await DBMemParagraph.get(id=wanted.id)).is_inactive is False, "名额只算真正捞回的"
        assert restore_done()

    async def test_hitting_the_limit_finishes_on_a_later_round(self) -> None:
        first = await _pruned(KnowledgeType.FACT)
        second = await _pruned(KnowledgeType.DECISION)

        assert await restore_pruned_stable(limit=1) == 1
        assert (await DBMemParagraph.get(id=first.id)).is_inactive is False
        assert not restore_done(), "撞上名额说明可能还有，不记标记"
        assert await restore_pruned_stable(limit=1) == 1
        assert (await DBMemParagraph.get(id=second.id)).is_inactive is False
        assert restore_done()

    async def test_consecutive_failures_stop_the_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for _ in range(8):
            await _pruned(KnowledgeType.FACT)
        calls: List[str] = []

        async def broken(text: str, **_kwargs: object) -> list[float]:
            calls.append(text)
            raise RuntimeError("嵌入服务挂了")

        monkeypatch.setattr(embedding_service_module, "embed_text", broken)
        assert await restore_pruned_stable(max_consecutive_failures=3) == 0
        assert len(calls) == 3, "连续失败就停，不把每一段都打一遍"
        assert not restore_done()


@pytest.mark.usefixtures("memory_env")
class TestManualPruneHasItsOwnLabel:
    async def test_manual_prune_is_labelled_and_still_shows_as_pruned(self) -> None:
        row = await _paragraph(workspace_id=3, knowledge_type=KnowledgeType.CONVERSATION, age_days=30)

        await maintenance.prune_workspace_memories(3, action=MANUAL_PRUNE_ACTION)

        fresh = await DBMemParagraph.get(id=row.id)
        assert (fresh.is_inactive, fresh.last_manual_action) == (True, MANUAL_PRUNE_ACTION)
        assert admin_service.paragraph_status(fresh) == "pruned", "后台照样显示为「已清理」"
        listed = await DBMemParagraph.filter(admin_service.status_q("pruned")).values_list("id", flat=True)
        assert row.id in listed
        deleted = await DBMemParagraph.filter(
            admin_service.status_q("deleted") & admin_service._not_pruned_q(),  # noqa: SLF001
        ).values_list("id", flat=True)
        assert row.id not in deleted

    async def test_unknown_action_is_refused(self) -> None:
        with pytest.raises(ValueError):
            await maintenance.prune_workspace_memories(3, action="delete")

    def test_both_manual_entry_points_pass_the_manual_label(self) -> None:
        from nekro_agent.routers import workspaces as workspaces_router
        from nekro_agent.services.command.built_in import ops

        assert "action=MANUAL_PRUNE_ACTION" in inspect.getsource(workspaces_router.prune_memory)
        assert "action=MANUAL_PRUNE_ACTION" in inspect.getsource(ops)
        assert "action=AUTO_PRUNE_ACTION" in inspect.getsource(maintenance.prune_all_workspaces)


class TestStopDuringTheFixup:
    @pytest.mark.asyncio
    async def test_cancel_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def cancelled() -> int:
            raise asyncio.CancelledError

        monkeypatch.setattr(scheduler_module, "extend_stable_half_life", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await MemoryMaintenanceScheduler()._extend_stable_half_life()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_no_prune_pass_after_stop_lands_in_the_fixup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_PRUNE_INTERVAL_HOURS", 0)
        monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", True)
        monkeypatch.setattr(scheduler_module, "PRUNE_MIN_INTERVAL_SECONDS", 0.01)
        scheduler = MemoryMaintenanceScheduler()
        pruned: List[int] = []

        async def stop_meanwhile() -> int:
            scheduler._running = False  # noqa: SLF001
            return 0

        async def no_restore() -> int:
            return 0

        async def fake_prune() -> dict[int, Any]:
            pruned.append(1)
            return {}

        monkeypatch.setattr(scheduler_module, "extend_stable_half_life", stop_meanwhile)
        monkeypatch.setattr(scheduler_module, "restore_pruned_stable", no_restore)
        monkeypatch.setattr(scheduler_module, "prune_all_workspaces", fake_prune)
        scheduler._running = True  # noqa: SLF001
        await asyncio.wait_for(scheduler._run_prune_loop(), timeout=2)  # noqa: SLF001
        assert pruned == [], "停下之后不再接着清理一整轮"


async def embedding_service_module_client() -> Any:
    from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module

    return await qdrant_manager_module.get_qdrant_client()


class TestPruneSwitchIsLive:
    @pytest.mark.asyncio
    async def test_switch_is_read_every_round(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        monkeypatch.setattr(config, "MEMORY_PRUNE_INTERVAL_HOURS", 0)
        monkeypatch.setattr(scheduler_module, "PRUNE_MIN_INTERVAL_SECONDS", 0.02)
        monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", False)
        calls: List[float] = []

        async def fake_prune() -> dict[int, Any]:
            calls.append(time.monotonic())
            return {}

        monkeypatch.setattr(scheduler_module, "prune_all_workspaces", fake_prune)
        scheduler = MemoryMaintenanceScheduler()
        scheduler._running = True  # noqa: SLF001
        loop = asyncio.create_task(scheduler._run_prune_loop())  # noqa: SLF001
        try:
            await asyncio.sleep(0.15)
            assert calls == [], "关着就不清理"
            monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", True)
            await asyncio.sleep(0.15)
            assert len(calls) >= 2, "打开后不用重启就开始清理"
            monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", False)
            await asyncio.sleep(0.05)
            stopped_at = len(calls)
            await asyncio.sleep(0.15)
            assert len(calls) == stopped_at, "关掉后下一轮起就不再清理"
        finally:
            scheduler._running = False  # noqa: SLF001
            loop.cancel()
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_loop_starts_even_when_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", False)
        monkeypatch.setattr(config, "MEMORY_STARTUP_RECOVERY_ENABLED", False)

        async def no_recovery(_self: MemoryMaintenanceScheduler, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(MemoryMaintenanceScheduler, "_recover_pending_consolidations", no_recovery)
        scheduler = MemoryMaintenanceScheduler()
        await scheduler.start()
        try:
            assert scheduler._prune_task is not None, "开关关着也起循环，打开时才不用重启"  # noqa: SLF001
        finally:
            await scheduler.stop()


class _RecordingLogger:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def __getattr__(self, level: str) -> Any:
        def record(message: str, *_args: object, **_kwargs: object) -> None:
            self.calls.append((level, str(message)))

        return record


class TestBackoffSkipIsNotLoggedAsFailure:
    @pytest.mark.asyncio
    async def test_backoff_skip_logs_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
        monkeypatch.setattr(config, "MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS", 0.01)
        monkeypatch.setattr(scheduler_module, "LOOP_IDLE_CHECK_SECONDS", 0.05)
        recorder = _RecordingLogger()
        monkeypatch.setattr(scheduler_module, "logger", recorder)

        async def no_db_backlog(_self: MemoryMaintenanceScheduler, _chat_key: str) -> None:
            return None

        monkeypatch.setattr(MemoryMaintenanceScheduler, "_db_backlog", no_db_backlog)

        retry_after = (datetime.now() + timedelta(hours=1)).isoformat()
        reasons = ["backoff", "timeout"]
        ran: List[str] = []

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            reason = reasons[len(ran)]
            ran.append(chat_key)
            return ConsolidationResult(failed_reason=reason, retry_after=retry_after)

        scheduler = MemoryMaintenanceScheduler()
        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        chats = ["web-session_backoff01", "web-session_timeout01"]
        for chat in chats:
            scheduler._chat_instance_map[chat] = ("web", "")  # noqa: SLF001
        scheduler._running = True  # noqa: SLF001
        loop = asyncio.create_task(scheduler._run_loop())  # noqa: SLF001
        try:
            for chat in chats:
                async with scheduler._lock:  # noqa: SLF001
                    await scheduler._schedule_consolidation(0, chat, ConsolidationPriority.NORMAL)  # noqa: SLF001
                deadline = time.monotonic() + 2
                while chat not in ran and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
        finally:
            scheduler._running = False  # noqa: SLF001
            scheduler._wakeup.set()  # noqa: SLF001
            loop.cancel()
            for timer, _due in list(scheduler._retry_timers.values()):  # noqa: SLF001
                timer.cancel()
            await asyncio.sleep(0)

        assert ran == chats
        backoff_lines = [(level, text) for level, text in recorder.calls if chats[0] in text]
        assert any(level == "info" and "退避期" in text for level, text in backoff_lines)
        assert not any("沉淀失败" in text for _level, text in backoff_lines), "退避中跳过不是新的失败"
        timeout_lines = [(level, text) for level, text in recorder.calls if chats[1] in text]
        assert any(level == "warning" and "沉淀失败" in text for level, text in timeout_lines), "真失败照旧告警"
