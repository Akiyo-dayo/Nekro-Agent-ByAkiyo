"""重启之后，库里的积压要能自己消化完

调度器的待沉淀数只在内存里，重启后从 0 算起。改造前「批次取满且待沉淀数 > 0 才续排」按内存计数判断，
重启后第一批取满了也不续排；启动恢复默认又是关的，于是积压只能等后续新消息一批一批带。实际部署里换版后
一个群积压上千条，按当时的流量要十几个小时才消得完。

这里用真表（sqlite）和一个按游标往前推的假沉淀器：每次从游标之后取最多一批，推进游标，返回取了几条。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory.consolidator import ConsolidationResult
from nekro_agent.services.memory.scheduler import MemoryMaintenanceScheduler

BATCH = 50
CHAT_BIG = "onebot_v11-backlog-group_1"
CHAT_SMALL = "onebot_v11-backlog-group_2"
CHAT_NO_CURSOR = "onebot_v11-backlog-group_3"


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_SIZE", BATCH)
    # 比批大小低：完整启动恢复的门槛会把 30 条的频道也算进去，兜底那一档（批大小 + 1）不会
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 20)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_STARTUP_RECOVERY_ENABLED", False)
    monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", False)
    monkeypatch.setattr(scheduler_module, "RECOVERY_START_DELAY_SECONDS", 0, raising=False)

    async def no_warm_up() -> None:
        return None

    monkeypatch.setattr(scheduler_module, "warm_up_tokenizer_async", no_warm_up)


async def seed(chat_key: str, message_count: int, *, with_cursor: bool = True) -> None:
    data: Dict[str, Any] = {"memory_state": {"last_consolidated_message_db_id": 0}} if with_cursor else {}
    await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key="onebot_v11",
        instance_key="backlog",
        channel_id=chat_key.rsplit("_", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        is_active=True,
        workspace_id=None,
        data=json.dumps(data),
    )
    await DBChatMessage.bulk_create(
        [
            DBChatMessage(
                sender_id="u1",
                sender_name="群友",
                sender_nickname="群友",
                is_tome=0,
                is_recalled=False,
                adapter_key="onebot_v11",
                instance_key="backlog",
                message_id=f"{chat_key}-{index}",
                chat_key=chat_key,
                chat_type="group",
                platform_userid="u1",
                content_text="积压里的一条消息",
                content_data="[]",
                raw_cq_code="",
                ext_data="{}",
                send_timestamp=1_700_000_000 + index,
            )
            for index in range(message_count)
        ],
    )


async def backlog(chat_key: str) -> int:
    channel = await DBChatChannel.get(chat_key=chat_key)
    cursor = json.loads(channel.data)["memory_state"]["last_consolidated_message_db_id"]
    return await DBChatMessage.filter(chat_key=chat_key, id__gt=cursor).count()


def cursor_handler(calls: List[tuple[str, int]]):
    """假沉淀器：从游标之后取最多一批，推进游标"""

    async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
        channel = await DBChatChannel.get(chat_key=chat_key)
        data = json.loads(channel.data)
        cursor = data["memory_state"]["last_consolidated_message_db_id"]
        ids = list(
            await DBChatMessage.filter(chat_key=chat_key, id__gt=cursor)
            .order_by("id")
            .limit(BATCH)
            .values_list("id", flat=True),
        )
        calls.append((chat_key, len(ids)))
        if not ids:
            return ConsolidationResult(skipped_reason="no_new_messages")
        data["memory_state"]["last_consolidated_message_db_id"] = ids[-1]
        channel.data = json.dumps(data)
        await channel.save()
        return ConsolidationResult(messages_processed=len(ids))

    return handler


async def wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return await predicate()


class TestFullBatchesKeepGoing:
    async def test_backlog_far_above_the_memory_count_is_drained(self, memory_db: None) -> None:
        del memory_db
        await seed(CHAT_BIG, 130)
        scheduler = MemoryMaintenanceScheduler()
        calls: List[tuple[str, int]] = []
        scheduler.set_consolidation_handler(cursor_handler(calls))  # type: ignore[arg-type]
        scheduler._running = True  # noqa: SLF001
        loop = asyncio.create_task(scheduler._run_loop())  # noqa: SLF001
        try:
            # 重启后的第一条新消息：内存里只记得这 1 条，库里游标之后是 130 条
            await scheduler.on_new_message(CHAT_BIG, 0, adapter_key="onebot_v11", instance_key="backlog")

            async def drained() -> bool:
                return await backlog(CHAT_BIG) == 0

            assert await wait_until(drained, timeout=5), calls
            assert [count for _, count in calls] == [50, 50, 30]
            stats = scheduler._channel_stats[(0, CHAT_BIG)]  # noqa: SLF001
            assert stats.pending_messages == 0, "待沉淀数以库里游标之后的实际条数为准"
        finally:
            await scheduler.stop()
            loop.cancel()


class TestDbBacklog:
    async def test_counts_messages_after_the_cursor_and_ignores_chats_without_one(self, memory_db: None) -> None:
        del memory_db
        await seed(CHAT_BIG, 7)
        await seed(CHAT_NO_CURSOR, 9, with_cursor=False)
        scheduler = MemoryMaintenanceScheduler()

        assert await scheduler._db_backlog(CHAT_BIG) == 7  # noqa: SLF001
        # 没有游标说明还没真正开始沉淀，开启之前的历史不回溯
        assert await scheduler._db_backlog(CHAT_NO_CURSOR) is None  # noqa: SLF001
        assert await scheduler._db_backlog("onebot_v11-backlog-group_missing") is None  # noqa: SLF001


class TestStartupPassWithoutRecovery:
    async def test_only_channels_over_one_batch_are_queued(self, memory_db: None) -> None:
        del memory_db
        await seed(CHAT_BIG, 120)
        await seed(CHAT_SMALL, 30)
        await seed(CHAT_NO_CURSOR, 200, with_cursor=False)
        scheduler = MemoryMaintenanceScheduler()
        try:
            await scheduler._recover_pending_consolidations(min_pending=BATCH + 1, max_tasks=0)  # noqa: SLF001

            assert [entry.chat_key for entry in scheduler.queued_tasks()] == [CHAT_BIG]
            assert scheduler._channel_stats[(0, CHAT_BIG)].pending_messages == 120  # noqa: SLF001
        finally:
            await scheduler.stop()

    async def test_restart_drains_a_large_backlog_by_itself(self, memory_db: None) -> None:
        del memory_db
        await seed(CHAT_BIG, 130)
        await seed(CHAT_SMALL, 30)
        scheduler = MemoryMaintenanceScheduler()
        calls: List[tuple[str, int]] = []
        scheduler.set_consolidation_handler(cursor_handler(calls))  # type: ignore[arg-type]
        await scheduler.start()
        try:

            async def drained() -> bool:
                return await backlog(CHAT_BIG) == 0

            assert await wait_until(drained, timeout=6), calls
            assert [count for chat, count in calls if chat == CHAT_BIG] == [50, 50, 30]
            # 积压不到一批的频道照旧等新消息带，启动时不补跑
            assert await backlog(CHAT_SMALL) == 30
            assert all(chat != CHAT_SMALL for chat, _ in calls)
        finally:
            await scheduler.stop()
