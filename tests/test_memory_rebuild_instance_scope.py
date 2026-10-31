"""记忆重建的**写入侧**实例作用域回归。

## 与 #5d 是同一件事的另一半

`#5d` 修的是读出口（`memory_tools` 三个沙盒方法漏了实例过滤）。这里是**写入口**：

`_replay_cc_semantic_memories` 会把工作区里所有 `CC_TO_NA` 通讯日志重新沉淀成语义记忆。
这些日志有两个来源：

- 控制台里用户直发 CC 指令 —— `source_chat_key == "__user__"`，不属于任何实例；
- `cc_workspace` 插件从某个频道委托出去的任务 —— `source_chat_key` 就是该频道的 `chat_key`
  （插件里 `task_id = chat_key`，`_deliver_pending_result` 原样写进日志）。

改造前这里**不传 `instance_key`**，`persist_cc_task_memory` 的默认值是空串，而空串正是
`instance_scope.LEGACY_INSTANCE_KEY` —— `resolve_allowed_instance_keys` 给**每一个**实例的
允许集合里都补了它（为存量数据留的口子）。于是：

> 管理员点一次「清空并重建工作区记忆库」，实例 A 频道里 CC 产出的结论，
> 就变成了同工作区所有实例都能检索到的记忆。

注意这不是"泄漏面变大"，是**凭空制造**：实时链路根本不会把频道来源的 CC 结果沉淀成语义记忆，
只有重建会。改前改后的差别可以直接在 `instance_key` 列上读出来。

单实例部署（`instance_key` 为空）与用户直发路径逐字不变：仍然落空串。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.services.memory import qdrant_manager as qdrant_module
from nekro_agent.services.memory import rebuild as rebuild_module
from nekro_agent.services.memory import rebuild_state_store as store_module
from nekro_agent.services.memory import semantic_writer as writer_module

WORKSPACE_ID = 9401
CHAT_A = "onebot_v11-ob9401a-group_1"
CHAT_B = "onebot_v11-ob9401b-group_2"
INSTANCE_A = "ob9401a"
INSTANCE_B = "ob9401b"

RESULT_A = "甲实例频道里 CC 得出的私密结论，长度必须超过最小沉淀阈值才会落库。" * 3
RESULT_B = "乙实例频道里 CC 得出的私密结论，长度必须超过最小沉淀阈值才会落库。" * 3
RESULT_USER = "控制台里用户直发 CC 指令得到的结论，长度同样超过最小沉淀阈值。" * 3


@pytest.fixture(autouse=True)
def isolated_rebuild_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把重建作业的状态文件写到 tmp_path，不污染仓库 data 目录。"""
    monkeypatch.setattr(store_module, "REBUILD_WORKSPACE_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(store_module, "REBUILD_JOB_DIR", tmp_path / "jobs")


@pytest.fixture(autouse=True)
def memory_system_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)


@pytest.fixture(autouse=True)
def no_vector_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量化与 Qdrant 写入在本用例里无关，屏蔽掉以免依赖外部服务。

    注意屏蔽的是 `semantic_writer` 自己 import 进来的那两个名字，不是被 import 的模块——
    换个地方打桩就会漏掉真正被调用的那一个。
    """

    async def fake_embed(_text: str) -> List[float]:
        return [0.0, 0.0, 0.0]

    async def fake_upsert(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(writer_module, "embed_text", fake_embed)
    monkeypatch.setattr(writer_module.memory_qdrant_manager, "upsert_paragraph", fake_upsert)
    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "delete_by_workspace", fake_upsert)


@pytest.fixture
async def rebuild_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def seed_channel(chat_key: str, instance_key: str, *, preset_id: int | None = None) -> None:
    await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key="onebot_v11",
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        is_active=True,
        workspace_id=WORKSPACE_ID,
        preset_id=preset_id,
        data=json.dumps({}),
    )


async def seed_preset(name: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=f"{name} 的设定",
        description=name,
        tags="",
        author="test",
    )


async def seed_cc_exchange(
    task_id: str,
    source_chat_key: str,
    result: str,
    request_text: str | None = None,
) -> None:
    await DBWorkspaceCommLog.create(
        workspace_id=WORKSPACE_ID,
        direction="NA_TO_CC",
        source_chat_key=source_chat_key,
        content=request_text or f"请处理任务 {task_id}",
        task_id=task_id,
    )
    await DBWorkspaceCommLog.create(
        workspace_id=WORKSPACE_ID,
        direction="CC_TO_NA",
        source_chat_key=source_chat_key,
        content=result,
        task_id=task_id,
    )


async def run_replay() -> int:
    state = store_module.create_job_state(
        WORKSPACE_ID,
        requested_by="tester",
        request_id="req-9401",
        cutoff=None,
    )
    channels_file = store_module.MemoryRebuildChannelsFile()
    state.snapshot.semantic_log_count_total = await DBWorkspaceCommLog.filter(
        workspace_id=WORKSPACE_ID,
        direction="CC_TO_NA",
    ).count()
    return await rebuild_module._replay_cc_semantic_memories(state, channels_file)  # noqa: SLF001


class TestResolveLogInstanceKey:
    """零件：来源频道 → 隔离标签 `(实例标识, 人设标识)` 的换算。"""

    @pytest.mark.asyncio
    async def test_channel_chat_key_resolves_to_its_instance(self, rebuild_db) -> None:
        await seed_channel(CHAT_A, INSTANCE_A)
        cache: dict[str, tuple[str, int]] = {}
        assert await rebuild_module._resolve_log_scope(CHAT_A, cache) == (INSTANCE_A, 0)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_user_pseudo_key_stays_global(self, rebuild_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """`__user__` 是伪键，不是频道标识：直接判定为"不属于任何实例"，**不查库**。

        「不查库」这条也要钉：拿伪键去查频道表恰好也查不到、返回值一模一样，
        但重建会为工作区里每一条用户直发的 CC 日志白跑一次查询（实测这个变异只靠返回值杀不掉）。
        """

        async def explode(*_args, **_kwargs):
            raise AssertionError("伪键不应该去查频道表")

        monkeypatch.setattr(DBChatChannel, "get_or_none", explode)
        cache: dict[str, tuple[str, int]] = {}
        assert await rebuild_module._resolve_log_scope("__user__", cache) == ("", 0)  # noqa: SLF001
        assert await rebuild_module._resolve_log_scope("", cache) == ("", 0)  # noqa: SLF001
        assert await rebuild_module._resolve_log_scope(None, cache) == ("", 0)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_unknown_channel_falls_back_to_global(self, rebuild_db) -> None:
        """频道行已经被删掉时无从判断归属，落空串与改造前一致，不臆造一个实例。"""
        cache: dict[str, tuple[str, int]] = {}
        assert await rebuild_module._resolve_log_scope("onebot_v11-gone-group_9", cache) == ("", 0)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_cache_avoids_repeat_lookup(self, rebuild_db, monkeypatch: pytest.MonkeyPatch) -> None:
        await seed_channel(CHAT_A, INSTANCE_A)
        cache: dict[str, tuple[str, int]] = {}
        assert await rebuild_module._resolve_log_scope(CHAT_A, cache) == (INSTANCE_A, 0)  # noqa: SLF001

        async def explode(*_args, **_kwargs):
            raise AssertionError("同一个 chat_key 不应该再查一次库")

        monkeypatch.setattr(DBChatChannel, "get_or_none", explode)
        assert await rebuild_module._resolve_log_scope(CHAT_A, cache) == (INSTANCE_A, 0)  # noqa: SLF001


class TestResolveLogPreset:
    """零件的另一半：人设维。

    它**没有**空串那样的兜底——落成 0（无人设档）时，绑了人设的频道从此检索不到这批
    重建出来的记忆，一次重建就把它们变成孤儿。而实例维那半即使写错也还有
    `LEGACY_INSTANCE_KEY` 顶着，两者的失败形态不一样，必须各测各的。
    """

    @pytest.mark.asyncio
    async def test_channel_preset_is_carried_into_the_scope(self, rebuild_db) -> None:
        preset = await seed_preset("人设甲")
        await seed_channel(CHAT_A, INSTANCE_A, preset_id=preset.id)
        cache: dict[str, tuple[str, int]] = {}
        assert await rebuild_module._resolve_log_scope(CHAT_A, cache) == (INSTANCE_A, preset.id)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_replayed_memory_is_tagged_with_the_channel_preset(self, rebuild_db) -> None:
        """接线：真的重放入口落库的 `preset_id` 必须跟着来源频道的人设走。"""
        preset = await seed_preset("人设甲")
        await seed_channel(CHAT_A, INSTANCE_A, preset_id=preset.id)
        await seed_cc_exchange("cc_delegate:a", CHAT_A, RESULT_A)

        assert await run_replay() == 1

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        assert [(row.instance_key, row.preset_id) for row in rows] == [(INSTANCE_A, preset.id)]

    @pytest.mark.asyncio
    async def test_channel_without_a_preset_still_lands_on_zero(self, rebuild_db) -> None:
        """对照组：没绑人设的频道仍然落 0，与改造前逐字相同。"""
        await seed_channel(CHAT_A, INSTANCE_A)
        await seed_cc_exchange("cc_delegate:a", CHAT_A, RESULT_A)

        assert await run_replay() == 1

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        assert [(row.instance_key, row.preset_id) for row in rows] == [(INSTANCE_A, 0)]


class TestReplayKeepsInstanceScope:
    """接线：真表 + 真的重放入口，落库的 `instance_key` 必须跟着来源频道走。"""

    @pytest.mark.asyncio
    async def test_channel_sourced_results_keep_their_instance(self, rebuild_db) -> None:
        await seed_channel(CHAT_A, INSTANCE_A)
        await seed_channel(CHAT_B, INSTANCE_B)
        await seed_cc_exchange("cc_delegate:a", CHAT_A, RESULT_A)
        await seed_cc_exchange("cc_delegate:b", CHAT_B, RESULT_B)

        assert await run_replay() == 2

        by_instance = {
            row.instance_key: row.content
            for row in await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        }
        # 改前两条都落在 "" 上，实例 B 一问"之前聊过什么"就能读到 A 的结论
        assert set(by_instance) == {INSTANCE_A, INSTANCE_B}
        assert RESULT_A[:20] in by_instance[INSTANCE_A]
        assert RESULT_B[:20] in by_instance[INSTANCE_B]

    @pytest.mark.asyncio
    async def test_user_initiated_results_stay_workspace_wide(self, rebuild_db) -> None:
        """控制台直发的 CC 任务不属于任何实例，仍然落空串（对所有实例可见），行为不变。"""
        await seed_cc_exchange("cc_user:1", "__user__", RESULT_USER)

        assert await run_replay() == 1

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        assert [row.instance_key for row in rows] == [""]

    @pytest.mark.asyncio
    async def test_single_instance_deployment_unchanged(self, rebuild_db) -> None:
        """单实例适配器的频道 `instance_key` 为空串，重放结果与改造前逐字相同。"""
        await seed_channel(CHAT_A, "")
        await seed_cc_exchange("cc_delegate:solo", CHAT_A, RESULT_A)

        assert await run_replay() == 1

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        assert [row.instance_key for row in rows] == [""]

class TestReplayDoesNotCollapseTasks:
    """`task_id` **不是每个任务一个**，拿它当 `origin_ref` 会把整批结果去重掉。

    `cc_workspace` 插件写的是 `task_id=f"cc_delegate:{chat_key}"`（每频道恒定），控制台
    直发路径则根本不写 `task_id`（整批 NULL）。于是改造前：

    - `origin_ref=reply_log.task_id` → 同一频道的第 2..N 条 CC 结果全被
      `persist_cc_task_memory` 的去重判成"已存在"直接丢掉（实测 3 条委托只落 1 条，
      而进度计数与 `semantic_replayed` 事件照样报 3 —— 又一次"日志说 N、数据落 1"）；
    - 请求日志按 `task_id` 取 `.first()` → 每条记忆都带着**最早**那个任务的正文。
    """

    @pytest.mark.asyncio
    async def test_every_delegation_from_one_channel_lands(self, rebuild_db) -> None:
        await seed_channel(CHAT_A, INSTANCE_A)
        plugin_task_id = f"cc_delegate:{CHAT_A}"
        for n in (1, 2, 3):
            await seed_cc_exchange(
                plugin_task_id,
                CHAT_A,
                f"第 {n} 个结论正文，长度必须超过最小沉淀阈值才会落库。" * 3,
                request_text=f"第 {n} 个委托任务的正文",
            )

        assert await run_replay() == 3

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).order_by("id").all()
        # 改前只有 1 条
        assert len(rows) == 3
        assert len({row.origin_ref for row in rows}) == 3
        assert all(row.instance_key == INSTANCE_A for row in rows)

    @pytest.mark.asyncio
    async def test_each_result_keeps_its_own_task_text(self, rebuild_db) -> None:
        """摘要取自任务正文：三条记忆的摘要必须分别是第 1/2/3 个任务，不是三份第 1 个。"""
        await seed_channel(CHAT_A, INSTANCE_A)
        plugin_task_id = f"cc_delegate:{CHAT_A}"
        for n in (1, 2, 3):
            await seed_cc_exchange(
                plugin_task_id,
                CHAT_A,
                f"第 {n} 个结论正文，长度必须超过最小沉淀阈值才会落库。" * 3,
                request_text=f"第 {n} 个委托任务的正文",
            )

        await run_replay()

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).order_by("id").all()
        assert [row.summary for row in rows] == [
            "第 1 个委托任务的正文",
            "第 2 个委托任务的正文",
            "第 3 个委托任务的正文",
        ]
        # 任务正文与结论正文必须是配对的那一组，不能张冠李戴
        for n, row in enumerate(rows, 1):
            assert f"第 {n} 个委托任务的正文" in row.content
            assert f"第 {n} 个结论正文" in row.content

    @pytest.mark.asyncio
    async def test_console_direct_send_logs_pair_up_too(self, rebuild_db) -> None:
        """控制台直发路径不写 `task_id`（全 NULL），同样不能全部配到第一条请求上。"""
        for n in (1, 2):
            await DBWorkspaceCommLog.create(
                workspace_id=WORKSPACE_ID,
                direction="USER_TO_CC",
                source_chat_key="__user__",
                content=f"控制台第 {n} 条指令",
            )
            await DBWorkspaceCommLog.create(
                workspace_id=WORKSPACE_ID,
                direction="CC_TO_NA",
                source_chat_key="__user__",
                content=f"控制台第 {n} 条结论正文，长度超过最小沉淀阈值。" * 3,
            )

        assert await run_replay() == 2

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).order_by("id").all()
        assert [row.summary for row in rows] == ["控制台第 1 条指令", "控制台第 2 条指令"]
        assert [row.instance_key for row in rows] == ["", ""]

    @pytest.mark.asyncio
    async def test_request_written_after_its_reply_still_pairs(self, rebuild_db) -> None:
        """请求日志排在回复**后面**时仍要配上——这条兜底保证不会比改造前少沉淀任何一条。

        日志是两个协程分别写的，顺序反过来并不稀奇；只按"前一条"找会直接漏掉整条记忆。
        """
        await DBWorkspaceCommLog.create(
            workspace_id=WORKSPACE_ID,
            direction="CC_TO_NA",
            source_chat_key="__user__",
            content="乱序结论正文，长度必须超过最小沉淀阈值才会落库。" * 3,
            task_id="out-of-order",
        )
        await DBWorkspaceCommLog.create(
            workspace_id=WORKSPACE_ID,
            direction="USER_TO_CC",
            source_chat_key="__user__",
            content="乱序请求正文",
            task_id="out-of-order",
        )

        assert await run_replay() == 1

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        assert [row.summary for row in rows] == ["乱序请求正文"]

    @pytest.mark.asyncio
    async def test_reply_without_any_request_log_is_skipped(self, rebuild_db) -> None:
        """孤儿回复（找不到任何请求日志）仍然跳过，不凭空造一条记忆。"""
        await DBWorkspaceCommLog.create(
            workspace_id=WORKSPACE_ID,
            direction="CC_TO_NA",
            source_chat_key="__user__",
            content="没有对应请求的结论正文，长度超过最小沉淀阈值。" * 3,
            task_id="orphan",
        )

        assert await run_replay() == 1
        assert await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).count() == 0


class TestReplayInstanceScopeEndToEnd:
    @pytest.mark.asyncio
    async def test_replayed_memory_is_not_visible_to_the_other_instance(self, rebuild_db) -> None:
        """把作用域判据接上：按乙实例的允许集合过滤，读不到甲实例重放出来的那条。"""
        from nekro_agent.services.memory.instance_scope import (
            build_instance_filter,
            resolve_allowed_instance_keys,
        )

        await seed_channel(CHAT_A, INSTANCE_A)
        await seed_channel(CHAT_B, INSTANCE_B)
        await seed_cc_exchange("cc_delegate:a", CHAT_A, RESULT_A)
        await run_replay()

        allowed_b = resolve_allowed_instance_keys(INSTANCE_B)
        visible = await DBMemParagraph.filter(
            workspace_id=WORKSPACE_ID,
            **build_instance_filter(allowed_b),
        ).all()
        assert visible == []

        allowed_a = resolve_allowed_instance_keys(INSTANCE_A)
        mine = await DBMemParagraph.filter(
            workspace_id=WORKSPACE_ID,
            **build_instance_filter(allowed_a),
        ).all()
        assert len(mine) == 1
