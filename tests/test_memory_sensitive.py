"""关于具体某人的敏感事：照样记，打标记，只在来源频道召回

所有者定的口径：群里聊到的收入、违规行为一类照记进长期记忆，但只在原群召回；跨频道开关
怎么设都一样，`memory_tools` 的手动搜索与详情也一样，后台管理照常看得到。

两半：
1. 沉淀：模型给每条记忆一个 `sensitive`，闸开的域行据此写 `is_sensitive`；正文照写。
2. 召回：`MemoryScope.apply` 回表时把来源频道以外的敏感段落挡掉（向量载荷里没有这一维）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _memory_privacy_harness import ADAPTER, I1, U151, UXW, Harness, MemoryEnv, SwitchState
from _memory_privacy_harness import memory_env as _shared_memory_env
from qdrant_client import AsyncQdrantClient

from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory.consolidator import EpisodicConsolidator, _truthy_flag
from nekro_agent.services.memory.instance_scope import resolve_memory_scope
from nekro_agent.services.memory.scope_condition import MemTable
from tests.test_memory_consolidation_quality import (  # noqa: F401 - 夹具按名字注入
    _channel,
    _install_llm,
    _install_persona,
    _item,
    _message,
    _run,
)
from tests.test_memory_consolidation_quality import memory_env as _quality_env

memory_env = _shared_memory_env
quality_env = _quality_env

ALL_OPEN = SwitchState(group_to_group="all", group_to_private="all", private_to_group="all")


async def _mark_sensitive(paragraph_id: int) -> None:
    await DBMemParagraph.filter(id=paragraph_id).update(is_sensitive=True)


class TestConsolidationWritesTheFlag:
    async def test_flag_follows_the_model_and_content_is_kept(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        chat_key = "onebot_v11-quality-group_7"
        await _channel(chat_key)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account="1234567", nickname="小满", content="今年年终奖一分没有")
        await _run(
            chat_key,
            monkeypatch,
            _item("小满说今年的年终奖为零", sensitive=True),
            _item("小满周末想去看展", sensitive=False),
            _item("小满最近在学吉他"),
        )

        rows = {row.content: row.is_sensitive for row in await DBMemParagraph.all()}
        assert rows == {
            "小满说今年的年终奖为零": True,
            "小满周末想去看展": False,
            "小满最近在学吉他": False,
        }, "敏感的照样记下，正文不改写"

    async def test_prompt_asks_for_the_flag(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        chat_key = "onebot_v11-quality-group_8"
        await _channel(chat_key)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account="1234567", nickname="小满", content="随便聊聊")
        prompts = _install_llm(monkeypatch, lambda _p: json.dumps({"memories": []}))
        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert '"sensitive": false' in prompts[0]
        assert "sensitive：" in prompts[0]
        assert "不要因此少记" in prompts[0], "标记只收窄召回范围，不是让模型少记"

    def test_string_true_counts(self) -> None:
        assert _truthy_flag(True) is True
        assert _truthy_flag("TRUE") is True
        assert _truthy_flag(" true ") is True
        assert _truthy_flag("yes") is False
        assert _truthy_flag(1) is False
        assert _truthy_flag(None) is False


async def _seed_group_secret(harness: Harness) -> tuple[Any, Any]:
    await harness.create_channel("gA", channel_type="group")
    secret = await harness.create_paragraph(
        content="151 说为了不被查办了副卡，麦当劳也用副卡付",
        origin_chat_key="gA",
        origin_chat_type="group",
        persons=(U151,),
    )
    await _mark_sensitive(secret.paragraph_id)
    plain = await harness.create_paragraph(
        content="151 中午吃了麦当劳",
        origin_chat_key="gA",
        origin_chat_type="group",
        persons=(U151,),
    )
    return secret, plain


class TestSensitiveStaysInItsChannel:
    async def test_other_group_never_sees_it_even_with_all_switches_open(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        secret, plain = await _seed_group_secret(harness)
        group_b = await harness.create_channel("gB", channel_type="group")
        harness.set_global(ALL_OPEN)

        assert await harness.retrieve_ids(group_b) == {plain.paragraph_id}, "开关全开，普通段落照常跨群"
        assert await harness.tool_search_ids(group_b) == {plain.paragraph_id}

    async def test_origin_group_still_recalls_it(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        secret, plain = await _seed_group_secret(harness)
        group_a = await DBChatChannel_get("gA")
        harness.set_global(SwitchState())

        assert await harness.retrieve_ids(group_a) == {secret.paragraph_id, plain.paragraph_id}
        assert await harness.tool_search_ids(group_a) == {secret.paragraph_id, plain.paragraph_id}

    async def test_private_chat_of_the_same_person_does_not_see_it(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        secret, plain = await _seed_group_secret(harness)
        private = await harness.create_channel("p151", channel_type="private")
        await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
        harness.set_global(ALL_OPEN)

        assert secret.paragraph_id not in await harness.retrieve_ids(private)
        assert secret.paragraph_id not in await harness.tool_search_ids(private)

    async def test_private_secret_does_not_reach_a_group(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        await harness.create_channel("p151", channel_type="private")
        await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
        secret = await harness.create_paragraph(
            content="151 私聊里说麦当劳的工资还没发",
            origin_chat_key="p151",
            origin_chat_type="private",
            persons=(U151,),
        )
        await _mark_sensitive(secret.paragraph_id)
        group = await harness.create_channel("gC", channel_type="group")
        await harness.create_message("gC", account=U151, seconds_ago=3600, index=1)
        harness.set_global(ALL_OPEN)

        assert secret.paragraph_id not in await harness.retrieve_ids(group)
        private = await DBChatChannel_get("p151")
        assert secret.paragraph_id in await harness.retrieve_ids(private), "来源私聊照常想得起来"

    async def test_tool_detail_and_trace_refuse_it_elsewhere(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        secret, _plain = await _seed_group_secret(harness)
        group_b = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=UXW, seconds_ago=3600, index=1)
        harness.set_global(ALL_OPEN)

        # 与任何看不见的段落一样按「不存在」处理，不提示它存在
        with pytest.raises(ValueError, match="未找到"):
            await harness.tool_detail(group_b, secret.paragraph_id)
        with pytest.raises(ValueError, match="未找到"):
            await harness.tool_trace(group_b, secret.paragraph_id)
        group_a = await DBChatChannel_get("gA")
        assert "副卡" in json.dumps(await harness.tool_detail(group_a, secret.paragraph_id), ensure_ascii=False)


SHARED_WORKSPACE = 9


async def _bind_same_workspace(*chat_keys: str) -> None:
    """绑同一个工作区：同工作区、同域的群互相在对方的「本地集」里"""
    from nekro_agent.models.db_chat_channel import DBChatChannel

    await DBChatChannel.filter(chat_key__in=list(chat_keys)).update(workspace_id=SHARED_WORKSPACE)


async def _relation(paragraph_id: Any, *, origin_chat_key: str) -> int:
    row = await DBMemRelation.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=I1,
        preset_id=0,
        origin_chat_key=origin_chat_key,
        subject_entity_id=1,
        predicate="办了",
        object_entity_id=2,
        paragraph_id=paragraph_id,
        memory_source="na",
        cognitive_type="episodic",
    )
    return int(row.id)


async def _episode(paragraph_ids: list[int], *, origin_chat_key: str) -> int:
    row = await DBMemEpisode.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=I1,
        preset_id=0,
        origin_chat_type="group",
        origin_chat_key=origin_chat_key,
        title="麦当劳",
        narrative_summary="拼进了段落正文的叙事",
        paragraph_ids=paragraph_ids,
    )
    await DBMemParagraph.filter(id__in=paragraph_ids).update(episode_id=row.id)
    return int(row.id)


async def _visible_ids(channel: Any, model: Any, table: MemTable) -> tuple[set[int], set[int]]:
    """同一个范围的两种写法（`apply` 与 `pg_q`）各取一遍"""
    scope = await resolve_memory_scope(channel)
    workspace_id = channel.workspace_id or 0
    via_apply = {int(row.id) for row in await scope.apply(model.all(), table, workspace_id=workspace_id)}
    via_q = {int(row.id) for row in await model.filter(scope.pg_q(table, workspace_id=workspace_id))}
    return via_apply, via_q


class TestSiblingGroupInTheSameWorkspace:
    """绑了同一个工作区的兄弟群在彼此的本地集里；敏感段落仍只在说出它的那个群召回"""

    async def test_sibling_group_does_not_recall_it_with_switches_off(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        secret, plain = await _seed_group_secret(harness)
        await harness.create_channel("gB", channel_type="group")
        await _bind_same_workspace("gA", "gB")
        group_a, group_b = await DBChatChannel_get("gA"), await DBChatChannel_get("gB")
        harness.set_global(SwitchState())

        assert await harness.retrieve_ids(group_b) == {plain.paragraph_id}, "普通段落照常在兄弟群可见"
        assert await harness.tool_search_ids(group_b) == {plain.paragraph_id}
        with pytest.raises(ValueError, match="未找到"):
            await harness.tool_detail(group_b, secret.paragraph_id)
        assert await harness.retrieve_ids(group_a) == {secret.paragraph_id, plain.paragraph_id}

    async def test_relations_and_episodes_built_from_it_stay_home(self, memory_env: MemoryEnv) -> None:
        harness = memory_env.harness
        secret, plain = await _seed_group_secret(harness)
        await harness.create_channel("gB", channel_type="group")
        await _bind_same_workspace("gA", "gB")
        group_a, group_b = await DBChatChannel_get("gA"), await DBChatChannel_get("gB")
        harness.set_global(ALL_OPEN)

        from_secret = await _relation(secret.paragraph_id, origin_chat_key="gA")
        from_plain = await _relation(plain.paragraph_id, origin_chat_key="gA")
        without_paragraph = await _relation(None, origin_chat_key="gA")
        with_secret = await _episode([secret.paragraph_id], origin_chat_key="gA")
        other_plain = await harness.create_paragraph(
            content="151 下午去打球",
            origin_chat_key="gA",
            origin_chat_type="group",
            persons=(U151,),
        )
        plain_only = await _episode([other_plain.paragraph_id], origin_chat_key="gA")

        for via in await _visible_ids(group_b, DBMemRelation, MemTable.RELATION):
            assert via == {from_plain, without_paragraph}, "来源段落为空的关系照常可见"
        for via in await _visible_ids(group_b, DBMemEpisode, MemTable.EPISODE):
            assert via == {plain_only}
        for via in await _visible_ids(group_a, DBMemRelation, MemTable.RELATION):
            assert via == {from_secret, from_plain, without_paragraph}
        for via in await _visible_ids(group_a, DBMemEpisode, MemTable.EPISODE):
            assert via == {with_secret, plain_only}


async def DBChatChannel_get(chat_key: str) -> Any:  # noqa: N802 - 与模型类名对齐，读着顺
    from nekro_agent.models.db_chat_channel import DBChatChannel

    return await DBChatChannel.get(chat_key=chat_key)
