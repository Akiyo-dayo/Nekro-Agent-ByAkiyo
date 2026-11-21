"""时间窗口与时间戳列的时区一致性回归

Tortoise 的 `auto_now_add` / `auto_now` 按 **ORM 配置的时区**写入带时区的值，而
`datetime.now()` 取的是**进程本地时区**的朴素值。两者不是同一种东西：

- SQLite 把 datetime 存成字符串、按字典序比较。ORM 时区与进程本地时区不一致时
  （`Tortoise.init` 不传 `timezone` 就默认 UTC，正是本测试套件的用法），一个朴素
  cutoff 会整体错开一个时区偏移量——在 CST+8 的机器上实测把"窗口内 4 小时"的那行
  静默漏掉，`get_hot_memories` 返回的热点少了一条。
- Postgres 侧朴素值由 asyncpg 按进程本地时区补全，恰好与 ORM 时区一致时结果相同，
  所以这类缺陷在线上不报错、也不会被普通查询发现。

因此这里钉的是"窗口边界按**时刻**判定，与写入方用哪个时区表达无关"，以及"应用自己写的
时间戳列与 ORM 写的 `create_time` 是同一种表示"。测试注入固定时刻，不依赖真实时间流逝：
换回 `datetime.now()` 后注入失效，窗口落到真实"今天"，用例立即失败。
"""

from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import pytest
from tortoise import Tortoise, connections

from nekro_agent.models import db_mem_reinforcement_log as reinforcement_module
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_mem_reinforcement_log import (
    DBMemReinforcementLog,
    TargetType,
    TriggerSource,
)
from nekro_agent.services.affection import store as store_module
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.stage import AffectionEventSource
from nekro_agent.services.affection.store import apply_delta, invalidate_cache
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain

WORKSPACE_ID = 4201

# fixture 不给 Tortoise 传 timezone，ORM 因此跑在 UTC 上
ORM_TZ = timezone.utc

# 固定基准时刻。取一个与"运行测试的那一天"无关的过去时刻：一旦窗口起点回退成
# `datetime.now()`，注入就不再生效，这些行会整体掉出 7 天窗口。
FROZEN_NOW = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc)
CUTOFF_7D = FROZEN_NOW - timedelta(days=7)

TZ_SHANGHAI = timezone(timedelta(hours=8))
TZ_NEW_YORK = timezone(timedelta(hours=-4))


@pytest.fixture
async def memory_db():
    """内存 SQLite 上的真实表结构

    刻意不传 `timezone`，即 ORM 跑在 UTC 上——这正是本仓库其余测试的配置，也是朴素
    时间戳与 ORM 时间戳会分叉的那种配置。
    """
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["nekro_agent.models.db_mem_reinforcement_log", "nekro_agent.models.db_affection"]},
    )
    await Tortoise.generate_schemas()
    invalidate_cache()
    try:
        yield
    finally:
        invalidate_cache()
        await Tortoise.close_connections()


async def seed_reinforcement(target_id: int, created_at: datetime) -> None:
    """插入一条强化日志，并把 `create_time` 改写成指定时刻

    `auto_now_add` 会覆盖创建时传入的值，所以落库后再改写；改写时统一折算到 ORM 时区，
    与 `auto_now_add` 真正写进去的表示保持一致（SQLite 上是字符串比较，混着不同偏移量
    书写同一时刻会退化成比较墙上时间的数字，那是后端限制，不是本用例要钉的性质）。
    """
    row = await DBMemReinforcementLog.create(
        workspace_id=WORKSPACE_ID,
        target_type=TargetType.PARAGRAPH,
        target_id=target_id,
        trigger_source=TriggerSource.MANUAL,
    )
    await DBMemReinforcementLog.filter(id=row.id).update(create_time=created_at.astimezone(ORM_TZ))


async def test_hot_memories_window_boundary(memory_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """7 天窗口的起点是"基准时刻减 7 天"，落在其后（含相等）的才被数进来

    每行写成"相对 cutoff 偏移多少"；用不同时区书写只是为了说明入参时区不影响归属，
    真正决定去留的是时刻。
    """
    monkeypatch.setattr(reinforcement_module, "db_now", lambda: FROZEN_NOW)

    cases: List[Tuple[int, timedelta, timezone, bool]] = [
        (1, timedelta(days=-1), timezone.utc, False),  # 窗口外一整天
        (2, timedelta(hours=-4), TZ_SHANGHAI, False),  # 窗口外 4 小时
        (3, timedelta(seconds=-1), TZ_NEW_YORK, False),  # 差 1 秒错过
        (4, timedelta(0), TZ_SHANGHAI, True),  # 恰好等于 cutoff
        (5, timedelta(seconds=1), timezone.utc, True),
        (6, timedelta(hours=4), TZ_NEW_YORK, True),  # 窗口内 4 小时
        (7, timedelta(days=6), TZ_SHANGHAI, True),
    ]
    for target_id, offset, tz, _expected in cases:
        await seed_reinforcement(target_id, (CUTOFF_7D + offset).astimezone(tz))

    rows = await DBMemReinforcementLog.get_hot_memories(
        workspace_id=WORKSPACE_ID,
        target_type=TargetType.PARAGRAPH,
        days=7,
    )

    assert sorted(int(row["target_id"]) for row in rows) == [
        target_id for target_id, _offset, _tz, expected in cases if expected
    ]


async def test_hot_memories_window_length_follows_days_argument(memory_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口长度真的由 `days` 决定，而不是被某个固定偏移量遮住

    只有当窗口起点确实是"基准时刻减去 days 天"时，同一批数据在 1 天 / 7 天 / 30 天下
    才会给出这三种不同的结果；朴素时间戳带来的整体错位会让边界那几行的归属发生变化。
    """
    monkeypatch.setattr(reinforcement_module, "db_now", lambda: FROZEN_NOW)

    for target_id, days_ago in ((10, 0.5), (20, 3), (30, 20), (40, 100)):
        await seed_reinforcement(target_id, FROZEN_NOW - timedelta(days=days_ago))

    for days, expected in ((1, [10]), (7, [10, 20]), (30, [10, 20, 30]), (365, [10, 20, 30, 40])):
        rows = await DBMemReinforcementLog.get_hot_memories(
            workspace_id=WORKSPACE_ID,
            target_type=TargetType.PARAGRAPH,
            days=days,
        )
        assert sorted(int(row["target_id"]) for row in rows) == expected, f"days={days}"


async def test_affection_write_timestamps_match_orm_representation(
    memory_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """好感度自己写的两列必须与 ORM 写的 `create_time` 是同一种表示

    这两列在后台与提示词里当作"这条状态是什么时候生成的"来读。若写成朴素本地时间，
    同一列里就会混着带偏移与不带偏移两种值，SQLite 上连字典序都不再可比。
    """
    monkeypatch.setattr(store_module, "db_now", lambda: FROZEN_NOW)

    key = AffectionKey.of_domain(
        ScopeDomain.of_instance("onebot_v11", "ob10001"),
        person_uid="123456",
        preset_id=PRESET_NONE,
    )
    await apply_delta(
        key,
        delta=3,
        reason="测试",
        source=AffectionEventSource.LLM,
        state_text="它今天心情不错。",
    )

    record = await DBAffection.get(**key.as_columns())
    assert record.state_generated_at is not None
    assert record.last_interact_at is not None
    assert record.state_generated_at.utcoffset() is not None
    assert record.last_interact_at.utcoffset() is not None
    assert record.state_generated_at == FROZEN_NOW
    assert record.last_interact_at == FROZEN_NOW

    raw = await connections.get("default").execute_query_dict(
        "SELECT create_time, state_generated_at, last_interact_at FROM affection WHERE id = ?",
        [record.id],
    )
    # SQLite 存的是字符串：`create_time` 由 ORM 写，另两列由应用写，必须都带时区偏移
    for column, value in raw[0].items():
        assert isinstance(value, str)
        assert value.endswith("+00:00"), f"{column} 落库成了不带时区的值: {value!r}"


# ---------------------------------------------------------------------------
# 记忆段落的 `event_time`
#
# `#6` 那一轮定下的规矩是「凡是写进 `DatetimeField` 或拿去与它比较的时间，一律
# `tortoise.timezone.now()`」，但当时是照着「模型里自己 `.now()` 的地方」扫的，
# 两处**由调用方传进来**的时间漏掉了：
#
# - `semantic_writer.persist_cc_task_memory` 的 `event_time or datetime.now()`；
# - `consolidator._store_memory` 的 `datetime.fromtimestamp(send_timestamp)`（不带 tz）。
#
# `event_time` 会被检索侧的时间窗（`retrieve(time_from=..., time_to=...)`，aware）拿去比，
# 也会被 `compute_effective_weight` 折算成绝对秒。朴素值在 ORM 时区 ≠ 进程本地时区时整体
# 错开一个偏移量——测试套件跑的正是这种配置。
# ---------------------------------------------------------------------------

PARAGRAPH_MODELS = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_paragraph_person",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_chat_message",
]


@pytest.fixture
async def paragraph_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": PARAGRAPH_MODELS})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def memory_writer_env(monkeypatch: pytest.MonkeyPatch):
    """打开记忆总开关并屏蔽向量化/Qdrant 副作用。"""
    from nekro_agent.core.config import config as app_config
    from nekro_agent.services.memory import consolidator as consolidator_module
    from nekro_agent.services.memory import semantic_writer as writer_module

    monkeypatch.setattr(app_config, "MEMORY_ENABLE_SYSTEM", True)

    async def fake_embed(_text: str, **_kwargs: object):
        return [0.0, 0.0, 0.0]

    async def fake_upsert(**_kwargs) -> bool:
        return True

    for module in (writer_module, consolidator_module):
        monkeypatch.setattr(module, "embed_text", fake_embed)
        monkeypatch.setattr(module.memory_qdrant_manager, "upsert_paragraph", fake_upsert)
    return None


async def read_raw_event_time(paragraph_id: int) -> str:
    rows = await connections.get("default").execute_query_dict(
        "SELECT event_time FROM mem_paragraph WHERE id = ?",
        [paragraph_id],
    )
    return rows[0]["event_time"]


async def test_cc_semantic_memory_event_time_is_aware(
    paragraph_db,
    memory_writer_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC 语义记忆不传 `event_time` 时，落库的必须是 `tortoise.timezone.now()` 给的那个时刻。

    **判据不能只看 `tzinfo`**：本仓库的 Tortoise 跑在 `use_tz=True` 下，`DatetimeField.to_db_value`
    对朴素值会 `make_aware(value, "UTC")` —— 也就是把「进程本地时区的墙上时间」原样当成 UTC
    重新解释，落库后 `tzinfo` 照样是 UTC，只是**时刻整体偏了一个时区**（本机实测偏 7 小时）。
    只断言"带时区"的话，退回 `datetime.now()` 的变异能活着通过（第一版就是这么写的，实测存活）。
    所以这里注入固定时刻：换回 `datetime.now()` 注入立刻失效，值对不上。
    """
    from nekro_agent.services.memory import semantic_writer as writer_module
    from nekro_agent.services.memory.semantic_writer import persist_cc_task_memory

    monkeypatch.setattr(writer_module, "db_now", lambda: FROZEN_NOW)

    paragraph = await persist_cc_task_memory(
        workspace_id=WORKSPACE_ID,
        task_content="整理一下记忆系统的实例作用域",
        result_content="结论正文，长度需要超过最小沉淀阈值才会落库。" * 5,
    )
    assert paragraph is not None
    assert paragraph.event_time == FROZEN_NOW
    assert paragraph.event_time.utcoffset() is not None
    assert (await read_raw_event_time(paragraph.id)).endswith("+00:00")


async def test_cc_semantic_memory_keeps_explicit_event_time(
    paragraph_db,
    memory_writer_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方传了 `event_time` 就用它——重建重放靠这条把原始日志时间带回来。"""
    from nekro_agent.services.memory import semantic_writer as writer_module
    from nekro_agent.services.memory.semantic_writer import persist_cc_task_memory

    monkeypatch.setattr(writer_module, "db_now", lambda: FROZEN_NOW)
    explicit = FROZEN_NOW - timedelta(days=3)

    paragraph = await persist_cc_task_memory(
        workspace_id=WORKSPACE_ID,
        task_content="重放一条历史任务",
        result_content="结论正文，长度需要超过最小沉淀阈值才会落库。" * 5,
        event_time=explicit,
    )
    assert paragraph is not None
    assert paragraph.event_time == explicit


async def test_consolidated_paragraph_event_time_is_aware_and_same_instant(
    paragraph_db,
    memory_writer_env,
) -> None:
    """情景记忆的 `event_time` 由锚点消息的 unix 时间戳换算而来。

    换算结果既要带时区，又必须仍然是**同一个时刻**——这条把「顺手改成 UTC 就行」
    和「把本地墙上时间当成 UTC 重新解释」区分开：后者会整体平移一个时区偏移量。
    """
    from nekro_agent.models.db_chat_message import DBChatMessage
    from nekro_agent.services.memory.consolidator import EpisodicConsolidator

    anchor_ts = 1773000000
    chat_key = "onebot_v11-obtz1-group_1"
    messages = [
        await DBChatMessage.create(
            message_id=f"{chat_key}-{idx}",
            sender_id="tz-sender",
            sender_name="tester",
            sender_nickname="tester",
            platform_userid="tz-user",
            is_tome=0,
            is_recalled=False,
            adapter_key="onebot_v11",
            instance_key="obtz1",
            chat_key=chat_key,
            chat_type="group",
            content_text="hello",
            content_data="[]",
            raw_cq_code="",
            ext_data="{}",
            send_timestamp=anchor_ts + idx,
        )
        for idx in range(2)
    ]

    consolidator = EpisodicConsolidator(WORKSPACE_ID)
    consolidator.instance_key = "obtz1"
    paragraph, _entities, _relations = await consolidator._store_memory(  # noqa: SLF001
        {
            "content": "一段沉淀出来的情景记忆正文",
            "summary": "情景记忆摘要",
            "knowledge_type": "conversation",
            "entities": [],
            "relations": [],
        },
        chat_key,
        messages,
    )
    assert paragraph is not None
    assert paragraph.event_time is not None
    assert paragraph.event_time.utcoffset() is not None
    # 同一个时刻：改成"把本地墙上时间标成 UTC"会在这里差出一个时区偏移量
    assert int(paragraph.event_time.timestamp()) == anchor_ts + 1
    assert (await read_raw_event_time(paragraph.id)).endswith("+00:00")
