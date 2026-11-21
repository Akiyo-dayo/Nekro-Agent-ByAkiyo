"""表情库的可读 / 可写集合

## 为什么这些断言必须逐条钉住

表情库改造后「会话能看到什么」完全由可读集合决定，而它是**算出来的**：可用池来自分配表，
再叠加当前人设、插件配置（`ENABLED_SETS` / `PERSONA_SET_RULES`）与实例归属。任何一处算错，
后果都不是报错而是**静默**：租户会话要么看到别人的表情，要么看不到自己该看的。

这里用一张小部署把每一档都摆出来：平台方实例、两个租户实例、一个没有实例段的频道，
四种分配目标各一条，人设两条。每条用例只改一个变量，断言集合相等而不是「包含」——
多给一个套组与少给一个是同一类事故。

## 口径

- 可读 = 本实例收集库 ∪ 分给当前人设的 ∪ 本实例挑出来的（与可用池取交集）；
- 可写 = 只有本实例收集库；
- 平台方提供的套组对租户只读；租户看不到「分给平台方实例」的那一条；
- 配置里写了池外的套组 ID 一律忽略（配置不能给自己授权）；
- 多租户部署解析不了归属时全空，单租户部署按平台方但**没有**收集库。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services import emotion_library as lib
from nekro_agent.services.resource_scope import resolve_chat_owner

ADAPTER = "onebot_v11"
PLATFORM_INSTANCE = "plat1"
TENANT_INSTANCE = "acme1"
RIVAL_INSTANCE = "rival1"
TENANT_ID = 11
RIVAL_TENANT_ID = 12

CHAT_PLATFORM = f"{ADAPTER}-{PLATFORM_INSTANCE}-group_300"
CHAT_TENANT = f"{ADAPTER}-{TENANT_INSTANCE}-group_100"
CHAT_RIVAL = f"{ADAPTER}-{RIVAL_INSTANCE}-group_200"
CHAT_NO_INSTANCE = "email-ops@example.com"
CHAT_GHOST = f"{ADAPTER}-ghost-group_999"


def ctx_of(chat_key: Optional[str], *, one_time_code: str = "") -> Any:
    """构造一个只带宿主签发频道标识的上下文

    `one_time_code` 是 `AgentCtx` 的只读属性（由进程内的会话令牌决定），缓存用例因此用一个
    最小替身：它只需要 `from_chat_key` 与 `one_time_code` 两个属性。
    """
    if one_time_code:
        return _CacheCtx(from_chat_key=chat_key, one_time_code=one_time_code)
    return AgentCtx(from_chat_key=chat_key, container_key="scope-probe")


class _CacheCtx:
    """只带两个属性的替身：`resolve_session_owner` 只读 `from_chat_key`，缓存只读 `one_time_code`"""

    def __init__(self, *, from_chat_key: Optional[str], one_time_code: str) -> None:
        self.from_chat_key = from_chat_key
        self.one_time_code = one_time_code


async def seed(tenants: bool) -> None:
    layout = (
        (PLATFORM_INSTANCE, None, CHAT_PLATFORM),
        (TENANT_INSTANCE, TENANT_ID if tenants else None, CHAT_TENANT),
        (RIVAL_INSTANCE, RIVAL_TENANT_ID if tenants else None, CHAT_RIVAL),
    )
    for instance_key, tenant_id, chat_key in layout:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
            chat_key=chat_key,
            channel_type="group",
            data="{}",
        )
    # 没有实例段的频道：按平台方对待，但没有「本实例」
    await DBChatChannel.create(
        adapter_key="email",
        instance_key="",
        channel_id=CHAT_NO_INSTANCE,
        chat_key=CHAT_NO_INSTANCE,
        channel_type="private",
        data="{}",
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    lib.clear_scope_cache()
    try:
        await seed(tenants=True)
        yield
    finally:
        lib.clear_scope_cache()
        await Tortoise.close_connections()


@pytest.fixture
async def single_tenant_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    lib.clear_scope_cache()
    try:
        await seed(tenants=False)
        yield
    finally:
        lib.clear_scope_cache()
        await Tortoise.close_connections()


async def scope_of(
    chat_key: Optional[str],
    *,
    enabled_sets: Sequence[int] = (),
    persona_set_rules: Sequence[Tuple[int, Sequence[int]]] = (),
) -> lib.EmotionScope:
    return await lib.resolve_emotion_scope(
        ctx_of(chat_key),
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )


async def make_sets() -> Dict[str, Any]:
    """四种分配目标各一条，外加两条人设分配与一个收集库"""

    async def preset(name: str) -> Any:
        return await lib.create_preset_set(name=name, category="分类")

    to_all = await preset("给所有实例")
    to_platform = await preset("给平台方实例")
    to_instance = await preset("给 acme1")
    to_persona = await preset("给人设甲")
    unassigned = await preset("没有分配")
    legacy = await lib.ensure_legacy_set()

    await lib.add_assignment(to_all.id, target=lib.TARGET_ALL_INSTANCES)
    await lib.add_assignment(to_platform.id, target=lib.TARGET_PLATFORM_INSTANCES)
    await lib.add_assignment(
        to_instance.id,
        target=lib.TARGET_INSTANCE,
        adapter_key=ADAPTER,
        instance_key=TENANT_INSTANCE,
    )
    persona = await DBPreset.create(
        name="甲",
        title="甲",
        avatar="",
        content="内容",
        description="",
        tags="",
        author="test",
    )
    await lib.add_assignment(to_persona.id, target=lib.TARGET_PRESET, preset_id=persona.id)
    return {
        "to_all": to_all,
        "to_platform": to_platform,
        "to_instance": to_instance,
        "to_persona": to_persona,
        "unassigned": unassigned,
        "legacy": legacy,
        "persona": persona,
    }


class TestPlatformInstance:
    async def test_platform_instance_reads_its_own_pool_and_writes_only_its_collect_set(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(CHAT_PLATFORM)

        assert scope.resolved is True
        assert scope.is_platform_instance is True
        assert scope.persona_id == 0
        assert scope.collect_set_id is not None
        # 平台方实例：收集库 ∪ 所有实例 ∪ 平台方实例（实例目标不指向它，人设 0 不匹配）
        assert scope.readable_set_ids == frozenset(
            {scope.collect_set_id, sets["to_all"].id, sets["to_platform"].id, sets["legacy"].id},
        )
        assert scope.writable_set_ids == frozenset({scope.collect_set_id})


class TestTenantInstance:
    async def test_tenant_instance_does_not_see_the_platform_instances_assignment(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(CHAT_TENANT)

        assert scope.is_platform_instance is False
        assert sets["to_platform"].id not in scope.readable_set_ids
        assert sets["legacy"].id not in scope.readable_set_ids
        # 收集库 ∪ 所有实例 ∪ 点名给这个实例的
        assert scope.readable_set_ids == frozenset(
            {scope.collect_set_id, sets["to_all"].id, sets["to_instance"].id},
        )
        assert scope.writable_set_ids == frozenset({scope.collect_set_id})

    async def test_another_tenants_instance_assignment_does_not_leak(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(CHAT_RIVAL)

        assert sets["to_instance"].id not in scope.readable_set_ids
        assert scope.readable_set_ids == frozenset({scope.collect_set_id, sets["to_all"].id})

    async def test_a_platform_set_is_readable_but_never_writable(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(CHAT_TENANT)

        assert sets["to_all"].id in scope.readable_set_ids
        assert sets["to_all"].id not in scope.writable_set_ids
        assert scope.writable_set_ids == frozenset({scope.collect_set_id})


class TestPersonaAssignment:
    async def test_persona_assignment_enters_readable_only_for_that_persona(self, db) -> None:
        sets = await make_sets()
        # 频道绑上人设甲
        await DBChatChannel.filter(chat_key=CHAT_TENANT).update(preset_id=sets["persona"].id)

        bound = await scope_of(CHAT_TENANT)
        assert bound.persona_id == sets["persona"].id
        assert sets["to_persona"].id in bound.readable_set_ids

        other = await scope_of(CHAT_RIVAL)
        assert other.persona_id == 0
        assert sets["to_persona"].id not in other.readable_set_ids

    async def test_persona_sets_are_readable_even_when_not_in_the_pool(self, db) -> None:
        """人设套组直接进可读：它由平台方点名分给这个人设，不经过可用池"""
        sets = await make_sets()
        await DBChatChannel.filter(chat_key=CHAT_TENANT).update(preset_id=sets["persona"].id)
        scope = await scope_of(CHAT_TENANT, enabled_sets=[sets["to_all"].id])

        assert sets["to_persona"].id in scope.readable_set_ids
        assert scope.readable_set_ids == frozenset(
            {scope.collect_set_id, sets["to_all"].id, sets["to_persona"].id},
        )


class TestPluginConfig:
    async def test_enabled_sets_narrows_the_pool_and_drops_ids_outside_it(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(
            CHAT_TENANT,
            enabled_sets=[sets["to_all"].id, sets["unassigned"].id, 999_999],
        )

        # 池外的（没有分配、不存在的）一律忽略；收集库始终在可读里
        assert scope.readable_set_ids == frozenset({scope.collect_set_id, sets["to_all"].id})

    async def test_empty_enabled_sets_means_the_whole_pool(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(CHAT_TENANT, enabled_sets=[])

        assert scope.readable_set_ids == frozenset(
            {scope.collect_set_id, sets["to_all"].id, sets["to_instance"].id},
        )

    async def test_persona_rules_take_precedence_over_enabled_sets(self, db) -> None:
        sets = await make_sets()
        await DBChatChannel.filter(chat_key=CHAT_TENANT).update(preset_id=sets["persona"].id)
        scope = await scope_of(
            CHAT_TENANT,
            enabled_sets=[sets["to_all"].id],
            persona_set_rules=[(sets["persona"].id, [sets["to_instance"].id])],
        )

        # 用了人设规则就完全不用 ENABLED_SETS：只剩规则里那一套（再叠人设分配与收集库）
        assert sets["to_all"].id not in scope.readable_set_ids
        assert scope.readable_set_ids == frozenset(
            {scope.collect_set_id, sets["to_instance"].id, sets["to_persona"].id},
        )

    async def test_persona_rules_cannot_grant_sets_outside_the_pool(self, db) -> None:
        """人设规则同样只能在可用池里挑：写进规则的池外套组（没分配的、平台方实例专属的）被忽略"""
        sets = await make_sets()
        await DBChatChannel.filter(chat_key=CHAT_TENANT).update(preset_id=sets["persona"].id)
        scope = await scope_of(
            CHAT_TENANT,
            persona_set_rules=[(sets["persona"].id, [sets["unassigned"].id, sets["to_platform"].id, sets["to_all"].id])],
        )

        assert sets["unassigned"].id not in scope.readable_set_ids
        assert sets["to_platform"].id not in scope.readable_set_ids
        assert scope.readable_set_ids == frozenset(
            {scope.collect_set_id, sets["to_all"].id, sets["to_persona"].id},
        )

    async def test_persona_rules_for_another_persona_do_not_apply(self, db) -> None:
        sets = await make_sets()
        scope = await scope_of(
            CHAT_TENANT,
            enabled_sets=[sets["to_all"].id],
            persona_set_rules=[(sets["persona"].id, [sets["to_instance"].id])],
        )

        # 频道没有绑人设（人设 0），规则里那一条不生效，回到 ENABLED_SETS
        assert scope.persona_id == 0
        assert scope.readable_set_ids == frozenset({scope.collect_set_id, sets["to_all"].id})


class TestUnresolvableSessions:
    async def test_unresolvable_in_a_multi_tenant_deployment_sees_nothing(self, db) -> None:
        await make_sets()
        scope = await scope_of(CHAT_GHOST)

        assert scope.resolved is False
        assert scope.readable_set_ids == frozenset()
        assert scope.writable_set_ids == frozenset()
        assert scope.collect_set_id is None

    async def test_unresolvable_in_a_single_tenant_deployment_reads_platform_sets(self, single_tenant_db) -> None:
        await make_sets()
        scope = await scope_of(CHAT_GHOST)

        assert scope.resolved is False
        assert scope.is_platform_instance is True
        # 单租户：没有租户边界可越，按平台方能读「所有实例」与「平台方实例」的
        sets = await lib.list_sets_visible_to_instance(ADAPTER, "", is_platform_instance=True)
        assert {row.kind for row in sets} == {lib.KIND_PRESET, lib.KIND_LEGACY}
        assert scope.collect_set_id is None
        assert scope.writable_set_ids == frozenset()
        assert scope.readable_set_ids != frozenset()

    async def test_a_channel_without_an_instance_has_no_collect_set(self, db) -> None:
        await make_sets()
        scope = await scope_of(CHAT_NO_INSTANCE)

        assert scope.resolved is True
        assert scope.instance_key == ""
        assert scope.collect_set_id is None
        assert scope.writable_set_ids == frozenset()
        assert scope.readable_set_ids == frozenset(
            {
                (await lib.ensure_legacy_set()).id,
                (await lib.DBEmotionSet.filter(name="给所有实例").first()).id,  # type: ignore[union-attr]
                (await lib.DBEmotionSet.filter(name="给平台方实例").first()).id,  # type: ignore[union-attr]
            },
        )


class TestCollectSet:
    async def test_each_instance_gets_exactly_one_collect_set(self, db) -> None:
        first = await lib.get_or_create_collect_set(ADAPTER, TENANT_INSTANCE)
        second = await lib.get_or_create_collect_set(ADAPTER, TENANT_INSTANCE)
        other = await lib.get_or_create_collect_set(ADAPTER, RIVAL_INSTANCE)

        assert first.id == second.id
        assert first.id != other.id
        assert first.kind == lib.KIND_COLLECT
        assert first.name == lib.COLLECT_SET_DEFAULT_NAME
        assert (first.owner_adapter_key, first.owner_instance_key) == (ADAPTER, TENANT_INSTANCE)

    async def test_resolving_twice_does_not_create_a_second_collect_set(self, db) -> None:
        await scope_of(CHAT_TENANT)
        await scope_of(CHAT_TENANT)
        assert await lib.DBEmotionSet.filter(kind=lib.KIND_COLLECT).count() == 1


class TestScopeCache:
    async def test_one_time_code_cache_hits_the_same_result(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await make_sets()
        calls = {"count": 0}
        original = lib.get_or_create_collect_set

        async def counting(adapter_key: str, instance_key: str):
            calls["count"] += 1
            return await original(adapter_key, instance_key)

        monkeypatch.setattr(lib, "get_or_create_collect_set", counting)
        probe = _CacheCtx(from_chat_key=CHAT_TENANT, one_time_code="round-code-1")

        first = await lib.resolve_emotion_scope(probe, enabled_sets=[], persona_set_rules=[])
        second = await lib.resolve_emotion_scope(probe, enabled_sets=[], persona_set_rules=[])

        assert first == second
        assert calls["count"] == 1

    async def test_a_different_one_time_code_recomputes(self, db) -> None:
        await make_sets()
        first = await lib.resolve_emotion_scope(
            _CacheCtx(from_chat_key=CHAT_TENANT, one_time_code="round-a"),
            enabled_sets=[],
            persona_set_rules=[],
        )
        second = await lib.resolve_emotion_scope(
            _CacheCtx(from_chat_key=CHAT_TENANT, one_time_code="round-b"),
            enabled_sets=[],
            persona_set_rules=[],
        )

        assert first == second

    async def test_changing_enabled_sets_invalidates_the_cached_entry(self, db) -> None:
        sets = await make_sets()
        probe = _CacheCtx(from_chat_key=CHAT_TENANT, one_time_code="round-c")

        everything = await lib.resolve_emotion_scope(probe, enabled_sets=[], persona_set_rules=[])
        narrowed = await lib.resolve_emotion_scope(
            probe,
            enabled_sets=[sets["to_all"].id],
            persona_set_rules=[],
        )

        assert narrowed.readable_set_ids == frozenset({narrowed.collect_set_id, sets["to_all"].id})
        assert everything.readable_set_ids != narrowed.readable_set_ids


class TestResolveForChat:
    async def test_chat_entry_matches_the_session_entry(self, db) -> None:
        await make_sets()
        via_ctx = await scope_of(CHAT_TENANT)
        via_chat = await lib.resolve_emotion_scope_for_chat(CHAT_TENANT, enabled_sets=[], persona_set_rules=[])

        assert via_chat == via_ctx

    async def test_chat_entry_uses_the_same_owner_resolution(self, db) -> None:
        owner = await resolve_chat_owner(CHAT_TENANT)
        scope = await lib.resolve_emotion_scope_for_chat(CHAT_TENANT, enabled_sets=[], persona_set_rules=[])

        assert (scope.adapter_key, scope.instance_key) == (owner.adapter_key, owner.instance_key)
        assert scope.is_platform_instance is owner.platform
