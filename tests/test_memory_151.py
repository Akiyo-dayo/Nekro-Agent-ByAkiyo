"""151 的 17 条例子：每条一个用例函数，逐条断言自动检索、主动搜索与来源脱敏。

布置：实例 I1 自成一域。群 A（gA）、群 B（gB），151 的私聊 p151、小伟的私聊 pxw、
小红的私聊 pxh；账号 u151、u151b、uxw、uxh。M1 = 群 A 的域行「151 中午吃了麦当劳」，
来源类型 group，涉及的人 [u151]。

跨频道召回的段落只允许「概述」跨过来：搜索结果与详情里的来源频道为空，追溯不返回
锚点原话并给出跨频道标记；本频道的段落照常返回来源与原始消息。
"""

from __future__ import annotations

from typing import Any

from _memory_privacy_harness import (
    ADAPTER,
    I1,
    I2,
    I3,
    SEARCH_LIMIT,
    U151,
    U151B,
    UXH,
    UXW,
    Harness,
    MemoryEnv,
    ParagraphSeed,
    SwitchState,
)
from _memory_privacy_harness import memory_env as _shared_memory_env

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.services.identity_scope import resolve_memory_domain
from nekro_agent.services.memory.persons import reset_member_cache
from nekro_agent.services.memory.retriever import MemoryRecallQuery, compile_memories_for_context

memory_env = _shared_memory_env

MEMBER_SECONDS = 5 * 86400
STALE_SECONDS = 31 * 86400


def _set_switches(
    harness: Harness,
    *,
    group_to_group: str = "off",
    group_to_private: str = "off",
    private_to_group: str = "off",
) -> None:
    harness.reset_overrides()
    harness.set_global(
        SwitchState(
            group_to_group=group_to_group,
            group_to_private=group_to_private,
            private_to_group=private_to_group,
        ),
    )


async def _assert_recall(
    harness: Harness,
    channel: Any,
    expected: set[int],
    query: str = "麦当劳",
) -> set[int]:
    """① 自动检索、② memory_tools 搜索、以及纯 Python 求值三方一致。"""
    ids = await harness.retrieve_ids(channel, query)
    assert ids == expected
    tool_ids = await harness.tool_search_ids(channel, query)
    assert tool_ids == expected, "主动搜索与自动检索必须同一个范围"
    assert await harness.expected_ids(channel) == expected, "条件树的纯 Python 求值必须一致"
    return ids


async def _assert_cross_channel_redacted(
    harness: Harness,
    channel: Any,
    seed: ParagraphSeed,
) -> None:
    items = await harness.tool_search(channel)
    item = next(item for item in items if int(item["target_id"]) == seed.paragraph_id)
    assert item["origin_chat_key"] is None

    detail = await harness.tool_detail(channel, seed.paragraph_id)
    assert detail["metadata"]["origin_chat_key"] is None

    trace = await harness.tool_trace(channel, seed.paragraph_id)
    assert trace["cross_channel"] is True
    assert trace["origin_chat_key"] is None
    assert trace["anchor_msg_id"] is None
    assert trace["anchor_msg_id_start"] is None
    assert trace["anchor_msg_id_end"] is None
    assert trace["anchor_timestamp_start"] is None
    assert trace["anchor_timestamp_end"] is None
    assert trace["messages"] == []


async def _assert_local_source(
    harness: Harness,
    channel: Any,
    seed: ParagraphSeed,
    *,
    origin_chat_key: str,
    anchor_message_id: str,
) -> None:
    items = await harness.tool_search(channel)
    item = next(item for item in items if int(item["target_id"]) == seed.paragraph_id)
    assert item["origin_chat_key"] == origin_chat_key

    detail = await harness.tool_detail(channel, seed.paragraph_id)
    assert detail["metadata"]["origin_chat_key"] == origin_chat_key

    trace = await harness.tool_trace(channel, seed.paragraph_id)
    assert trace["cross_channel"] is False
    assert trace["origin_chat_key"] == origin_chat_key
    assert trace["messages"], "本频道段落的追溯必须照常返回锚点原话"
    assert trace["messages"][0]["message_id"] == anchor_message_id


async def _seed_m1(harness: Harness, *, with_anchor_message: bool = False) -> ParagraphSeed:
    await harness.create_channel("gA", channel_type="group")
    if with_anchor_message:
        await harness.create_message("gA", account=U151, seconds_ago=3600, index=1)
    return await harness.create_paragraph(
        content="151 中午吃了麦当劳",
        origin_chat_key="gA",
        origin_chat_type="group",
        persons=(U151,),
        anchor_msg_id="gA-m1" if with_anchor_message else None,
    )


async def _seed_m3(harness: Harness, *, with_anchor_message: bool = False) -> ParagraphSeed:
    await harness.create_channel("p151", channel_type="private")
    await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
    if with_anchor_message:
        await harness.create_message("p151", account=U151, seconds_ago=3600, index=2, chat_type="private")
    return await harness.create_paragraph(
        content="151 私聊里说晚上要加班",
        origin_chat_key="p151",
        origin_chat_type="private",
        persons=(U151,),
        anchor_msg_id="p151-m2" if with_anchor_message else None,
    )


async def _seed_group_domain(harness: Harness) -> DBInstanceGroup:
    group = await DBInstanceGroup.create(name="domain-151", tenant_id=1)
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, I1, memory_group=group)
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, I2, memory_group=group)
    return group


class Test151Examples:
    async def test_case_01_origin_group_always_recalls(self, memory_env: MemoryEnv) -> None:
        """第 1 条：群 A 问 M1，任意开关，召回；来源与锚点原话照常返回。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness, with_anchor_message=True)
        gA = await DBChatChannel.get(chat_key="gA")

        _set_switches(harness)
        await _assert_recall(harness, gA, {m1.paragraph_id})
        await _assert_local_source(harness, gA, m1, origin_chat_key="gA", anchor_message_id="gA-m1")

    async def test_case_02_group_to_group_off_does_not_recall(self, memory_env: MemoryEnv) -> None:
        """第 2 条：群 B 问 M1，G→G 关，151 在群 B，不召回。"""
        harness = memory_env.harness
        await _seed_m1(harness)
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="off")
        await _assert_recall(harness, gB, set())

    async def test_case_03_group_to_group_related_without_member_does_not_recall(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 3 条：群 B 问 M1，G→G 相关，151 不在群 B，不召回。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness)
        gB = await harness.create_channel("gB", channel_type="group")

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, set())
        assert m1.paragraph_id not in await harness.retrieve_ids(gB)

    async def test_case_04_group_to_group_related_with_member_recalls_and_redacts(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 4 条：群 B 问 M1，G→G 相关，151 五天前发过言，召回且来源脱敏。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness, with_anchor_message=True)
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, {m1.paragraph_id})
        await _assert_cross_channel_redacted(harness, gB, m1)

    async def test_case_05_stale_member_does_not_count(self, memory_env: MemoryEnv) -> None:
        """第 5 条：151 最后一次在群 B 发言是 31 天前，G→G 相关不召回。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness)
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=STALE_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, set())
        assert m1.paragraph_id not in await harness.retrieve_ids(gB)

    async def test_case_06_group_to_group_all_ignores_membership(self, memory_env: MemoryEnv) -> None:
        """第 6 条：群 B 问 M1，G→G 全部，151 不在群 B，仍然召回且来源脱敏。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness, with_anchor_message=True)
        gB = await harness.create_channel("gB", channel_type="group")

        _set_switches(harness, group_to_group="all")
        await _assert_recall(harness, gB, {m1.paragraph_id})
        await _assert_cross_channel_redacted(harness, gB, m1)

    async def test_case_07_requester_membership_in_group_a_does_not_matter(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 7 条：小伟是不是群 A 成员，不影响第 3 / 4 条的结果。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness)
        gB_without = await harness.create_channel("gB1", channel_type="group")
        gB_with = await harness.create_channel("gB2", channel_type="group")
        await harness.create_message("gB2", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        # 小伟不在群 A
        await _assert_recall(harness, gB_without, set())
        await _assert_recall(harness, gB_with, {m1.paragraph_id})

        # 小伟在群 A：提问的人变了，结果必须不变
        await harness.create_message("gA", account=UXW, seconds_ago=3600, index=2)
        await _assert_recall(harness, gB_without, set())
        await _assert_recall(harness, gB_with, {m1.paragraph_id})

    async def test_case_08_multi_person_memory_requires_everyone_in_group(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 8 条：M2 涉及 151 与小红，G→G 相关要求两人都是群 B 成员。"""
        harness = memory_env.harness
        await harness.create_channel("gA", channel_type="group")
        m2 = await harness.create_paragraph(
            content="151 和小红一起吃了麦当劳",
            origin_chat_key="gA",
            origin_chat_type="group",
            persons=(U151, UXH),
        )
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, set())

        await harness.create_message("gB", account=UXH, seconds_ago=MEMBER_SECONDS, index=2)
        reset_member_cache()
        await _assert_recall(harness, gB, {m2.paragraph_id})

    async def test_case_09_group_memory_into_private_chats(self, memory_env: MemoryEnv) -> None:
        """第 9 条：群记忆进私聊，关 / 相关 / 全部三档。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness, with_anchor_message=True)
        p151 = await harness.create_channel("p151", channel_type="private")
        pxw = await harness.create_channel("pxw", channel_type="private")
        await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
        await harness.create_message("pxw", account=UXW, seconds_ago=3600, index=1, chat_type="private")

        _set_switches(harness, group_to_private="off")
        await _assert_recall(harness, p151, set())
        await _assert_recall(harness, pxw, set())

        _set_switches(harness, group_to_private="related")
        await _assert_recall(harness, p151, {m1.paragraph_id})
        await _assert_recall(harness, pxw, set())
        await _assert_cross_channel_redacted(harness, p151, m1)

        _set_switches(harness, group_to_private="all")
        await _assert_recall(harness, p151, {m1.paragraph_id})
        await _assert_recall(harness, pxw, {m1.paragraph_id})

    async def test_case_10_group_memory_reaches_each_mentioned_person_only(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 10 条：M2 涉及 151 与小红，G→P 相关时只进这两人的私聊。"""
        harness = memory_env.harness
        await harness.create_channel("gA", channel_type="group")
        m2 = await harness.create_paragraph(
            content="151 和小红一起吃了麦当劳",
            origin_chat_key="gA",
            origin_chat_type="group",
            persons=(U151, UXH),
        )
        p151 = await harness.create_channel("p151", channel_type="private")
        pxh = await harness.create_channel("pxh", channel_type="private")
        pxw = await harness.create_channel("pxw", channel_type="private")
        for chat_key, account in (("p151", U151), ("pxh", UXH), ("pxw", UXW)):
            await harness.create_message(chat_key, account=account, seconds_ago=3600, index=1, chat_type="private")

        _set_switches(harness, group_to_private="related")
        await _assert_recall(harness, p151, {m2.paragraph_id})
        await _assert_recall(harness, pxh, {m2.paragraph_id})
        await _assert_recall(harness, pxw, set())

    async def test_case_11_private_memory_into_group(self, memory_env: MemoryEnv) -> None:
        """第 11 条：私聊记忆进群，关 / 相关（在 / 不在群）/ 全部。"""
        harness = memory_env.harness
        m3 = await _seed_m3(harness, with_anchor_message=True)
        gA = await harness.create_channel("gA", channel_type="group")
        gC = await harness.create_channel("gC", channel_type="group")
        await harness.create_message("gA", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, private_to_group="off")
        await _assert_recall(harness, gA, set())

        _set_switches(harness, private_to_group="related")
        await _assert_recall(harness, gA, {m3.paragraph_id})
        await _assert_cross_channel_redacted(harness, gA, m3)
        await _assert_recall(harness, gC, set())

        _set_switches(harness, private_to_group="all")
        await _assert_recall(harness, gC, {m3.paragraph_id})

    async def test_case_12_private_memory_never_enters_another_private_chat(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 12 条：小伟的私聊里，M3 在 27 种开关组合下永远不召回。"""
        harness = memory_env.harness
        m3 = await _seed_m3(harness)
        pxw = await harness.create_channel("pxw", channel_type="private")
        await harness.create_message("pxw", account=UXW, seconds_ago=3600, index=1, chat_type="private")

        for g2g in ("off", "related", "all"):
            for g2p in ("off", "related", "all"):
                for p2g in ("off", "related", "all"):
                    _set_switches(harness, group_to_group=g2g, group_to_private=g2p, private_to_group=p2g)
                    ids = await harness.retrieve_ids(pxw)
                    assert m3.paragraph_id not in ids, f"私聊记忆进了别人的私聊: {g2g}/{g2p}/{p2g}"

    async def test_case_13_alias_merge_and_group_domain_private_follow_person(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 13 条：副号私聊跟随合并，组域里另一个实例的私聊硬规则始终可见。"""
        harness = memory_env.harness
        group = await _seed_group_domain(harness)
        domain = await resolve_memory_domain(ADAPTER, I1)
        assert domain.group_id == group.id

        p151 = await harness.create_channel("p151", channel_type="private")
        p151b = await harness.create_channel("p151b", channel_type="private")
        await harness.create_channel("p151i2", instance_key=I2, channel_type="private")
        await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
        await harness.create_message("p151b", account=U151B, seconds_ago=3600, index=1, chat_type="private")
        await harness.create_message(
            "p151i2",
            account=U151,
            seconds_ago=3600,
            index=1,
            instance_key=I2,
            chat_type="private",
        )

        m3 = await harness.create_paragraph(
            content="151 私聊里说晚上要加班",
            origin_chat_key="p151",
            origin_chat_type="private",
            persons=(U151,),
        )
        m3_i2 = await harness.create_paragraph(
            content="151 在另一个实例的私聊里说周末去爬山",
            origin_chat_key="p151i2",
            origin_chat_type="private",
            persons=(U151,),
            instance_key=I2,
            anchor_msg_id="p151i2-m1",
        )

        _set_switches(harness)
        # 合并前：副号不是主号，召回不到；组域里另一个实例的私聊按硬规则可见。
        await _assert_recall(harness, p151b, set())
        await _assert_recall(harness, p151, {m3.paragraph_id, m3_i2.paragraph_id})
        await _assert_cross_channel_redacted(harness, p151, m3_i2)

        await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid=U151B,
            person_uid=U151,
        )
        # 合并后：副号的私聊立刻带上主号的人；另一个实例仍可见。
        await _assert_recall(harness, p151b, {m3.paragraph_id, m3_i2.paragraph_id})
        await _assert_recall(harness, p151, {m3.paragraph_id, m3_i2.paragraph_id})

        await DBIdentityAlias.filter(platform_userid=U151B).delete()
        # 撤销合并：下一次调用就生效，不需要清任何缓存。
        await _assert_recall(harness, p151b, set())
        await _assert_recall(harness, p151, {m3.paragraph_id, m3_i2.paragraph_id})

    async def test_case_14_other_tenant_never_recalls(self, memory_env: MemoryEnv) -> None:
        """第 14 条：另一租户的实例在三个开关全开时也召回不到 M1、M3。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness)
        m3 = await _seed_m3(harness)
        g3 = await harness.create_channel("g3", instance_key=I3, channel_type="group")
        p3 = await harness.create_channel("p3", instance_key=I3, channel_type="private")
        await harness.create_message("p3", account=U151, seconds_ago=3600, index=1, instance_key=I3, chat_type="private")

        _set_switches(harness, group_to_group="all", group_to_private="all", private_to_group="all")
        await _assert_recall(harness, g3, set())
        await _assert_recall(harness, p3, set())
        assert m1.paragraph_id not in await harness.retrieve_ids(g3)
        assert m3.paragraph_id not in await harness.retrieve_ids(p3)

    async def test_case_15_global_default_versus_instance_override(self, memory_env: MemoryEnv) -> None:
        """第 15 条：全局全部 + 实例关不召回；全局关 + 实例相关召回。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness)
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        harness.reset_overrides()
        harness.set_global(SwitchState(group_to_group="all"))
        harness.set_instance(I1, SwitchState(group_to_group="off"))
        await _assert_recall(harness, gB, set())

        harness.reset_overrides()
        harness.set_global(SwitchState(group_to_group="off"))
        harness.set_instance(I1, SwitchState(group_to_group="related"))
        await _assert_recall(harness, gB, {m1.paragraph_id})

    async def test_case_16_group_membership_by_sub_account_after_merge(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 16 条：151 只用副号在群 B 发言，合并前不召回、合并后召回、撤销后不召回。"""
        harness = memory_env.harness
        m1 = await _seed_m1(harness)
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151B, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, set())

        domain = await resolve_memory_domain(ADAPTER, I1)
        await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid=U151B,
            person_uid=U151,
        )
        # 成员集缓存的是原始账号集合 u151b；别名展开每次现查，所以合并立刻生效。
        await _assert_recall(harness, gB, {m1.paragraph_id})

        await DBIdentityAlias.filter(platform_userid=U151B).delete()
        await _assert_recall(harness, gB, set())

    async def test_case_17_memory_without_people_only_opens_in_all_mode(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        """第 17 条：M4 没有涉及的人，相关档不召回、全部档召回。"""
        harness = memory_env.harness
        await harness.create_channel("gA", channel_type="group")
        m4 = await harness.create_paragraph(
            content="群 A 里聊到周末天气",
            origin_chat_key="gA",
            origin_chat_type="group",
            persons=(),
        )
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, set())

        _set_switches(harness, group_to_group="all")
        await _assert_recall(harness, gB, {m4.paragraph_id})


class TestAliasReverseDirection:
    """第 13、16 条的反方向：记忆记在**副号**上，从**主号**那边来问。

    正方向（记在主号、从副号来问）只靠「副号 → 人」这一跳就能命中；反方向必须把「人 →
    他的全部别名账号」也展开出来，否则合并过的人在主号这边永远想不起副号时期的事。
    """

    async def test_private_memory_under_sub_account_follows_to_main_account(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        harness = memory_env.harness
        p151 = await harness.create_channel("p151", channel_type="private")
        await harness.create_channel("p151b", channel_type="private")
        await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
        await harness.create_message("p151b", account=U151B, seconds_ago=3600, index=1, chat_type="private")
        m_sub = await harness.create_paragraph(
            content="151 用副号私聊时说想吃麦当劳",
            origin_chat_key="p151b",
            origin_chat_type="private",
            persons=(U151B,),
        )

        _set_switches(harness)
        await _assert_recall(harness, p151, set())

        domain = await resolve_memory_domain(ADAPTER, I1)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=U151B, person_uid=U151)
        await _assert_recall(harness, p151, {m_sub.paragraph_id})

        await DBIdentityAlias.filter(platform_userid=U151B).delete()
        await _assert_recall(harness, p151, set())

    async def test_group_memory_under_sub_account_with_member_by_main_account(
        self,
        memory_env: MemoryEnv,
    ) -> None:
        harness = memory_env.harness
        await harness.create_channel("gA", channel_type="group")
        m_sub = await harness.create_paragraph(
            content="151 用副号在群 A 说中午吃了麦当劳",
            origin_chat_key="gA",
            origin_chat_type="group",
            persons=(U151B,),
        )
        gB = await harness.create_channel("gB", channel_type="group")
        await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)

        _set_switches(harness, group_to_group="related")
        await _assert_recall(harness, gB, set())

        domain = await resolve_memory_domain(ADAPTER, I1)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=U151B, person_uid=U151)
        await _assert_recall(harness, gB, {m_sub.paragraph_id})

        await DBIdentityAlias.filter(platform_userid=U151B).delete()
        await _assert_recall(harness, gB, set())


async def test_compiled_prompt_does_not_leak_origin_chat_key(memory_env: MemoryEnv) -> None:
    """自动注入的上下文块只写类别、时间与正文，不写来源频道。"""
    harness = memory_env.harness
    m1 = await _seed_m1(harness)
    gB = await harness.create_channel("gB", channel_type="group")
    await harness.create_message("gB", account=U151, seconds_ago=MEMBER_SECONDS, index=1)
    _set_switches(harness, group_to_group="related")
    memories = await harness.retrieve(gB)
    assert {int(memory.target_id) for memory in memories} == {m1.paragraph_id}

    context = await compile_memories_for_context(
        MemoryRecallQuery(query_text="麦当劳", focus_text="", focus_points=[], context_texts=[]),
        memories,
    )
    assert "麦当劳" in context
    assert "gA" not in context


def test_all_seventeen_cases_are_present() -> None:
    """清单自检：17 条各有一个用例函数，避免以后误删某条还静默通过。"""
    names = [name for name in dir(Test151Examples) if name.startswith("test_case_")]
    assert len(names) == 17
    assert SEARCH_LIMIT >= 10
