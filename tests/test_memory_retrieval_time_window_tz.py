"""检索时间窗的时区一致性回归（`#5h` 的**读出侧**另一半）。

`#6` 定的规矩是「凡是写进 `DatetimeField` **或拿去与它比较**的时间，一律带时区」。
round-16 把 `event_time` 的两处写入补齐了，但**拿去比较**的那一半没动：

`MemoryRetriever.retrieve(time_from=..., time_to=...)` 的入参**可能是朴素值**——
规划模型吐的 JSON（`recall_contract.RecallQuery.time_from` 是 `datetime | None`，
LLM 完全可能写成 `"2026-03-15T04:00:00"`）和管理端 query 参数里不带偏移量的 ISO 串
（`workspaces.py` 的 `datetime.fromisoformat(s.replace("Z", "+00:00"))`）都会解析成 naive。

而同一个朴素值在这一次检索里被**解释成两个不同的时刻**：

- 向量通道：`int(time_from.timestamp())` —— Python 对朴素值按**进程本地时区**折算，
  再拿去和 Qdrant payload 里的 `event_time`（由 aware 值算出的真 epoch）比；
- Episode 通道：把同一个值交给 Tortoise 与 `DatetimeField` 比较，`use_tz=True` 下
  `make_aware(value, "UTC")`，按 **UTC** 解释。

进程本地时区不是 UTC 时（线上默认就是 Asia/Shanghai），两条通道的时间窗差一个时区偏移量：
向量那条整体偏 8 小时，"最近三小时聊了什么"要么召回一批过期的，要么一条都召不回。

改法：`retrieve()` 入口统一走 `_normalize_datetime`（朴素 = UTC，与 ORM 口径一致），
再往下发。传 aware 值时是同一时刻的等价改写，**行为逐字不变**。

## 判据为什么这么写

"两条通道折算出同一个时刻"这条判据在**本机恰好是 UTC 时恒成立**——那样的话
"退回朴素值"的变异在 CI 上能活着通过（§2.9：同一份断言在两台机器上验的是两件事）。
所以这里钉两件与本机时区无关的事：① 真正下发给两条通道的都是 **aware** 值；
② 折算出来的 epoch 等于「把朴素值当 UTC」的那个值。①在任何机器上都能杀掉变异。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.retriever import MemoryRetriever

WORKSPACE_ID = 9501

# 与本机时区无关的固定时刻。窗口起点写成朴素值，正是缺陷的触发形态。
NAIVE_FROM = datetime(2026, 3, 15, 4, 0, 0)
EXPECTED_EPOCH = int(NAIVE_FROM.replace(tzinfo=timezone.utc).timestamp())

BEFORE = NAIVE_FROM.replace(tzinfo=timezone.utc) - timedelta(hours=12)
AFTER = NAIVE_FROM.replace(tzinfo=timezone.utc) + timedelta(hours=12)

TERM = "沉淀"


@pytest.fixture
async def memory_db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["nekro_agent.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def retrieval_env(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """打桩的只有两个**外部服务**边界：向量化与 Qdrant。

    时间窗的换算逻辑本身、Episode 通道的整条查询都跑真的。
    """
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    recorded: Dict[str, Any] = {}

    async def fake_embed(_text: str) -> List[float]:
        return [0.1, 0.2, 0.3]

    async def fake_search(**kwargs: Any) -> List[Any]:
        recorded["event_time_from"] = kwargs.get("event_time_from")
        recorded["event_time_to"] = kwargs.get("event_time_to")
        return []

    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(retriever_module.memory_qdrant_manager, "search", fake_search)

    # Episode 通道**不打替身**：包一层记录器再调回真身，真实查询照常执行。
    original = MemoryRetriever._retrieve_episode_memories  # noqa: SLF001

    async def recording_episode(self, *args: Any, **kwargs: Any):
        recorded["episode_time_from"] = kwargs.get("time_from")
        recorded["episode_time_to"] = kwargs.get("time_to")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryRetriever, "_retrieve_episode_memories", recording_episode)
    return recorded


async def seed_episode(title: str, start: datetime, end: datetime) -> DBMemEpisode:
    return await DBMemEpisode.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        origin_chat_key="onebot_v11-ob9501-group_1",
        title=title,
        narrative_summary=f"{title} 的叙述摘要",
        time_start=start,
        time_end=end,
        participant_entity_ids=[],
        paragraph_ids=[],
        phase_mapping={},
        base_weight=1.0,
    )


class TestNaiveWindowIsNormalized:
    @pytest.mark.asyncio
    async def test_both_channels_receive_aware_values(
        self,
        memory_db,
        retrieval_env: Dict[str, Any],
    ) -> None:
        """两条通道拿到的都必须是 aware 值，且是「朴素 = UTC」折算出来的那个时刻。

        改前：Episode 通道收到的就是原样的朴素值（`tzinfo is None`），
        向量通道收到的是按**进程本地时区**折算出来的 epoch。
        """
        await seed_episode(f"{TERM}前", BEFORE - timedelta(hours=1), BEFORE)

        retriever = MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted())
        await retriever.retrieve(TERM, time_from=NAIVE_FROM, record_access=False)

        episode_from = retrieval_env["episode_time_from"]
        assert episode_from is not None
        assert episode_from.utcoffset() is not None, "Episode 通道收到的仍是朴素值"
        assert int(episode_from.timestamp()) == EXPECTED_EPOCH
        assert retrieval_env["event_time_from"] == EXPECTED_EPOCH

    @pytest.mark.asyncio
    async def test_both_channels_agree_on_the_same_instant(
        self,
        memory_db,
        retrieval_env: Dict[str, Any],
    ) -> None:
        """向量通道下发的 epoch 与 Episode 通道下发的时刻必须是同一个瞬间。"""
        await seed_episode(f"{TERM}后", AFTER, AFTER + timedelta(hours=1))

        retriever = MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted())
        await retriever.retrieve(
            TERM,
            time_from=NAIVE_FROM,
            time_to=NAIVE_FROM + timedelta(days=1),
            record_access=False,
        )

        assert retrieval_env["event_time_from"] == int(retrieval_env["episode_time_from"].timestamp())
        assert retrieval_env["event_time_to"] == int(retrieval_env["episode_time_to"].timestamp())

    @pytest.mark.asyncio
    async def test_window_really_filters_episodes(
        self,
        memory_db,
        retrieval_env: Dict[str, Any],
    ) -> None:
        """端到端：窗口真的落到了 Episode 查询上，而不是被一路忽略。

        没有这条的话，上面两条即使全绿也可能是"时间窗压根没生效"。
        """
        await seed_episode(f"{TERM}前", BEFORE - timedelta(hours=1), BEFORE)
        late = await seed_episode(f"{TERM}后", AFTER, AFTER + timedelta(hours=1))

        retriever = MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted())
        result = await retriever.retrieve(TERM, time_from=NAIVE_FROM, record_access=False)
        assert [m.target_id for m in result.memories] == [late.id]

        # 对照组：不给窗口时两条都在——证明"只剩一条"是窗口起的作用，不是别的条件挡掉的
        unfiltered = await retriever.retrieve(TERM, record_access=False)
        assert len(unfiltered.memories) == 2


class TestAwareWindowUnchanged:
    """传 aware 值的调用方逐字不变（既有调用方与单部署形态走的就是这条）。"""

    @pytest.mark.asyncio
    async def test_aware_input_keeps_its_instant(
        self,
        memory_db,
        retrieval_env: Dict[str, Any],
    ) -> None:
        shanghai = timezone(timedelta(hours=8))
        aware_from = NAIVE_FROM.replace(tzinfo=shanghai)

        retriever = MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted())
        await retriever.retrieve(TERM, time_from=aware_from, record_access=False)

        # 与改造前 `int(aware_from.timestamp())` 逐字相同（aware 值本来就折算正确）
        assert retrieval_env["event_time_from"] == int(aware_from.timestamp())
        assert retrieval_env["episode_time_from"] == aware_from
