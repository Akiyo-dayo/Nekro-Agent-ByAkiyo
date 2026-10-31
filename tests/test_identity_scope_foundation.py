"""隔离键地基的契约测试：`(记忆域, 人, 人设)`

## 这份测试要钉死什么

1. **零配置 = 今天的行为，逐字。** 这是本轮最重要的一条回归保险。没有实例组、没有别名、
   频道没绑人设时，好感的隔离键必须退化成今天的 `(adapter_key, instance_key, platform_userid)`，
   记忆的允许集合必须逐字等于 `memory.instance_scope.resolve_allowed_instance_keys` 的输出
   （包括 `None` 这个"不过滤"的取值）。
2. **存量行原地成立。** 按旧 schema 语义写进去的好感行（两个新列取默认值），
   必须能被解析器算出来的键查到——落位是「自成一域 / 人 = 自己 / 无人设档」。
   落不到这个位置，测试部署上现有的数据就等于消失了。
3. **别名按域隔离。** 一个域里声明"这两个号是同一个人"绝不能泄进另一个域。
   这是跨租户串域这条风险面在数据层的落点。
4. **不做缓存。** 改域、合别名之后**下一次**解析立刻变。任何 TTL 都会让这组用例变红，
   这正是它们存在的理由（缓存策略的取舍写在 `services/identity_scope.py` 的模块文档里）。
5. **无人设档的取值定死为 0**，且人设维走的是 `get_preset()` 那条回退链、不是裸的
   `channel.preset_id`。

## 为什么用真表而不是假存储

隔离性是**数据层**的性质。用假存储验证等于把要保护的东西替换掉了——这条判据与
`tests/test_affection_system.py` 的固件同源。
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import INSTANCE_METADATA_DEFAULT_PRESET_ID, DBChatChannel
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import (
    SELF_DOMAIN_GROUP_ID,
    DBInstanceDomainBinding,
    DBInstanceGroup,
)
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.identity_scope import (
    PRESET_NONE,
    IdentityScope,
    ScopeDomain,
    resolve_affection_domain,
    resolve_domain_instance_keys,
    resolve_identity_scope,
    resolve_identity_scope_by_chat_key,
    resolve_identity_scopes,
    resolve_memory_domain,
    resolve_person_uid,
    resolve_person_uids,
)
from nekro_agent.services.memory.instance_scope import resolve_allowed_instance_keys
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE_A = "ob10001"
INSTANCE_B = "ob10002"
INSTANCE_C = "ob10003"
USER_MAIN = "123456"
USER_ALT = "654321"
OTHER_USER = "999999"


@pytest.fixture
async def scope_db():
    """内存 SQLite 上的真实表结构

    注册整个 `nekro_agent.models`：本轮新增的两个模型必须真的登记进那份清单，
    只注册被测模块会让"忘了在 `models/__init__.py` 里导出"这种漏永远测不出来。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class _StubChannel:
    """频道替身

    解析器只要三样东西（见 `identity_scope.ChannelLike`）。把 `DBChatChannel` 整个拉进来
    会连带引入插件收集器与适配器注册表，与被测契约无关；人设那条回退链另有
    `TestPresetDimension` 用真模型钉住。
    """

    def __init__(self, instance_key: str, *, preset_id: int = PRESET_NONE, adapter_key: str = ADAPTER) -> None:
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id


async def _bind(
    instance_key: str,
    *,
    memory_group_id: Optional[int] = None,
    affection_group_id: Optional[int] = None,
    adapter_key: str = ADAPTER,
) -> DBInstanceDomainBinding:
    return await DBInstanceDomainBinding.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        memory_group_id=memory_group_id,
        affection_group_id=affection_group_id,
    )


async def _make_group(name: str = "family", tenant_id: Optional[int] = None) -> DBInstanceGroup:
    return await DBInstanceGroup.create(name=name, tenant_id=tenant_id)


class TestZeroConfigIsByteForByteToday:
    """零配置的部署必须与今天**逐字**相同

    这组用例是本轮最重要的回归保险：整套隔离键改造的前提是"不配置就什么都没变"。
    """

    async def test_domain_of_an_unbound_instance_is_itself(self, scope_db: None) -> None:
        domain = await resolve_memory_domain(ADAPTER, INSTANCE_A)
        assert domain == ScopeDomain(SELF_DOMAIN_GROUP_ID, ADAPTER, INSTANCE_A)
        assert domain.is_group is False

    async def test_affection_columns_degrade_to_todays_triple(self, scope_db: None) -> None:
        """好感隔离键退化成今天的三元组：两个新列取默认值，其余三列逐字不变"""
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        assert scope.affection_columns() == {
            "adapter_key": ADAPTER,
            "instance_key": INSTANCE_A,
            "domain_group_id": 0,
            "platform_userid": USER_MAIN,
            "preset_id": 0,
        }

    @pytest.mark.parametrize("instance_key", ["", INSTANCE_A, INSTANCE_B])
    async def test_memory_allowed_keys_equal_the_legacy_function(self, scope_db: None, instance_key: str) -> None:
        """记忆允许集合逐字等于旧函数的输出，包括 `None`（不过滤）这一档

        单实例部署（空 `instance_key`）返回 `None` 是"改造前零影响"的那条，
        退化成 `[""]` 也会让检索多一个条件——形态上很像"没变"，实际是另一件事。
        """
        assert await resolve_domain_instance_keys(ADAPTER, instance_key) == resolve_allowed_instance_keys(instance_key)

    async def test_single_instance_deployment_gets_no_filter(self, scope_db: None) -> None:
        assert await resolve_domain_instance_keys("minecraft", "") is None

    async def test_person_is_the_account_itself(self, scope_db: None) -> None:
        domain = await resolve_memory_domain(ADAPTER, INSTANCE_A)
        assert await resolve_person_uid(domain, USER_MAIN) == USER_MAIN

    async def test_no_preset_binding_lands_on_preset_none(self, scope_db: None) -> None:
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        assert scope.preset_id == PRESET_NONE == 0


class TestLegacyRowsLandDeterministically:
    """测试部署上的存量好感行必须原地可读

    上线是全新启动、不做存量迁移，但**存量行不能因此消失**。落位定死为
    「自成一域 / 人 = 自己的 platform_userid / 无人设档」，靠的是两个新列的默认值，
    因此迁移是纯 DDL、不需要任何回填。
    """

    async def test_a_row_written_the_old_way_is_found_by_the_resolved_key(self, scope_db: None) -> None:
        # 按旧 schema 的写法建行：只填三个旧列，两个新列走默认值
        await DBAffection.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, platform_userid=USER_MAIN, score=42)

        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        record = await DBAffection.filter(**scope.affection_columns()).first()
        assert record is not None
        assert record.score == 42

    async def test_the_defaults_are_exactly_the_documented_landing(self, scope_db: None) -> None:
        await DBAffection.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, platform_userid=USER_MAIN)
        record = await DBAffection.get(adapter_key=ADAPTER, instance_key=INSTANCE_A, platform_userid=USER_MAIN)
        assert (record.domain_group_id, record.preset_id) == (SELF_DOMAIN_GROUP_ID, PRESET_NONE)

    async def test_legacy_rows_of_two_instances_do_not_collide(self, scope_db: None) -> None:
        """新唯一键是旧键的超集，存量行照旧互不冲突（迁移期不会撞约束）"""
        await DBAffection.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, platform_userid=USER_MAIN, score=1)
        await DBAffection.create(adapter_key=ADAPTER, instance_key=INSTANCE_B, platform_userid=USER_MAIN, score=2)
        assert await DBAffection.all().count() == 2


class TestScopeDomainEncoding:
    """域的两种编码只有一个产生者，且两种编码互相不打架"""

    @pytest.mark.parametrize(
        ("domain", "expected_id", "expected_columns"),
        [
            (
                ScopeDomain.of_instance(ADAPTER, INSTANCE_A),
                f"i:{ADAPTER}:{INSTANCE_A}",
                {"adapter_key": ADAPTER, "instance_key": INSTANCE_A, "domain_group_id": 0},
            ),
            (
                ScopeDomain.of_instance("minecraft", ""),
                "i:minecraft:",
                {"adapter_key": "minecraft", "instance_key": "", "domain_group_id": 0},
            ),
            (
                ScopeDomain.of_group(7),
                "g:7",
                {"adapter_key": "", "instance_key": "", "domain_group_id": 7},
            ),
        ],
    )
    def test_encodings(
        self,
        domain: ScopeDomain,
        expected_id: str,
        expected_columns: Dict[str, Any],
    ) -> None:
        assert domain.domain_id == expected_id
        assert domain.as_columns() == expected_columns

    def test_group_rows_never_carry_an_instance(self) -> None:
        """属于实例组时三列里的实例段必须是空串，否则同组两个实例会各写一行"""
        domain = ScopeDomain.of_group(3)
        assert domain.adapter_key == ""
        assert domain.instance_key == ""

    def test_a_self_domain_never_collides_with_a_group_domain(self) -> None:
        """两种编码在字符串与三列上都不会撞车"""
        self_domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        group_domain = ScopeDomain.of_group(1)
        assert self_domain.domain_id != group_domain.domain_id
        assert self_domain.as_columns() != group_domain.as_columns()

    @pytest.mark.parametrize("group_id", [0, -1])
    def test_non_positive_group_id_is_rejected(self, group_id: int) -> None:
        """非正组 ID 直接抛，不悄悄退化成自成一域（那会让一整组数据落到读不到的位置）"""
        with pytest.raises(ValueError, match="实例组 ID"):
            ScopeDomain.of_group(group_id)


class TestMemoryDomainResolution:
    """把几个实例放进同一个组 = 互通；组外的实例不受影响"""

    async def test_two_bound_instances_share_one_domain(self, scope_db: None) -> None:
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)

        domain_a = await resolve_memory_domain(ADAPTER, INSTANCE_A)
        domain_b = await resolve_memory_domain(ADAPTER, INSTANCE_B)
        assert domain_a == domain_b == ScopeDomain.of_group(group.id)

    async def test_an_instance_outside_the_group_stays_isolated(self, scope_db: None) -> None:
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_C) == ScopeDomain.of_instance(ADAPTER, INSTANCE_C)

    async def test_a_binding_row_with_all_nulls_equals_no_row(self, scope_db: None) -> None:
        """两个组字段都为 NULL 的行语义上等价于没有行，读侧不需要区分"""
        await _bind(INSTANCE_A)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)

    async def test_binding_is_scoped_by_adapter_too(self, scope_db: None) -> None:
        """`instance_key` 的唯一性只在 `(adapter_key, instance_key)` 上成立，域必须同样带上适配器

        两个适配器下同名的实例行是能同时存在的（见 `DBAdapterInstance.is_instance_active`
        的 docstring）。只按 `instance_key` 取域会随机命中一行，两个方向都是串域。
        """
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id, adapter_key="discord")
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        assert await resolve_memory_domain("discord", INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_a_dangling_group_id_still_resolves_to_that_group(self, scope_db: None) -> None:
        """组行被删掉但绑定还在时，域仍按该组成立

        回落成自成一域会让整组的好感与记忆当场消失，而这正是删组时最不该发生的事。
        """
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await group.delete()
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_one_instance_belongs_to_at_most_one_domain(self, scope_db: None) -> None:
        """数据库唯一约束兜住"每个实例属于且仅属于一个域"""
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        with pytest.raises(Exception):  # noqa: B017 - 不同后端抛的具体类型不同，这里只关心"被挡住了"
            await _bind(INSTANCE_A, memory_group_id=group.id)


class TestAffectionDomainOverride:
    """好感的可空域覆盖字段：默认跟随记忆域，前端不暴露"""

    async def test_affection_follows_the_memory_domain_by_default(self, scope_db: None) -> None:
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        assert await resolve_affection_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_override_splits_the_two_domains(self, scope_db: None) -> None:
        memory_group = await _make_group("mem")
        affection_group = await _make_group("aff")
        await _bind(INSTANCE_A, memory_group_id=memory_group.id, affection_group_id=affection_group.id)

        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        assert scope.domain == ScopeDomain.of_group(memory_group.id)
        assert scope.affection_domain == ScopeDomain.of_group(affection_group.id)

    async def test_affection_only_sharing_needs_no_schema_change(self, scope_db: None) -> None:
        """「好感互通但记忆不互通」只填好感字段即可，记忆仍自成一域"""
        affection_group = await _make_group("aff")
        await _bind(INSTANCE_A, affection_group_id=affection_group.id)
        await _bind(INSTANCE_B, affection_group_id=affection_group.id)

        scope_a = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        scope_b = await resolve_identity_scope(_StubChannel(INSTANCE_B), USER_MAIN)
        assert scope_a.affection_domain == scope_b.affection_domain == ScopeDomain.of_group(affection_group.id)
        assert scope_a.domain != scope_b.domain
        assert await resolve_domain_instance_keys(ADAPTER, INSTANCE_A) == resolve_allowed_instance_keys(INSTANCE_A)

    async def test_affection_group_members_include_both_halves(self, scope_db: None) -> None:
        """好感组的成员 = 显式覆盖的 ∪ 默认跟随的，少算任何一半都会漏人"""
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, affection_group_id=group.id)
        members = await DBInstanceDomainBinding.members_of_affection_group(group.id)
        assert sorted(members) == [(ADAPTER, INSTANCE_A), (ADAPTER, INSTANCE_B)]


class TestDomainInstanceKeysSupersetsTheLegacyFunction:
    """记忆允许集合：实例组带来的成员 ∪ 旧配置共享组，且永远含空串兜底"""

    async def test_group_members_are_all_allowed(self, scope_db: None) -> None:
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)

        allowed = await resolve_domain_instance_keys(ADAPTER, INSTANCE_A)
        assert allowed is not None
        assert set(allowed) == {INSTANCE_A, INSTANCE_B, ""}

    async def test_legacy_config_groups_keep_working(self, scope_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """已经配了 `MEMORY_INSTANCE_SHARING_GROUPS` 的部署不会在升级那一刻静默失去共享"""
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {"legacy": [INSTANCE_A, INSTANCE_C]})
        allowed = await resolve_domain_instance_keys(ADAPTER, INSTANCE_A)
        assert allowed is not None
        assert set(allowed) == {INSTANCE_A, INSTANCE_C, ""}

    async def test_db_group_and_config_group_are_unioned(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {"legacy": [INSTANCE_A, INSTANCE_C]})
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)

        allowed = await resolve_domain_instance_keys(ADAPTER, INSTANCE_A)
        assert allowed is not None
        assert set(allowed) == {INSTANCE_A, INSTANCE_B, INSTANCE_C, ""}

    async def test_an_instance_outside_the_group_is_unaffected(self, scope_db: None) -> None:
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)
        assert await resolve_domain_instance_keys(ADAPTER, INSTANCE_C) == resolve_allowed_instance_keys(INSTANCE_C)


class TestAliasesAreScopedToTheirDomain:
    """别名按域隔离——这是"跨租户串域"在数据层的落点，绝不能妥协"""

    async def test_an_alias_in_one_domain_does_not_leak_into_another(self, scope_db: None) -> None:
        domain_a = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        domain_b = ScopeDomain.of_instance(ADAPTER, INSTANCE_B)
        await DBIdentityAlias.create(domain_id=domain_a.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)

        assert await resolve_person_uid(domain_a, USER_ALT) == USER_MAIN
        # 另一个域里的同一个号必须还是他自己
        assert await resolve_person_uid(domain_b, USER_ALT) == USER_ALT

    async def test_group_domains_are_distinct_from_self_domains(self, scope_db: None) -> None:
        """自成一域的别名不会因为该实例后来进了组就跟着生效（域变了，键就变了）"""
        self_domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=self_domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        assert await resolve_person_uid(ScopeDomain.of_group(1), USER_ALT) == USER_ALT

    async def test_scope_resolution_uses_the_domain_not_the_instance(self, scope_db: None) -> None:
        """两个实例进同一个组之后，组域里的别名对两边同时成立"""
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)
        await DBIdentityAlias.create(
            domain_id=ScopeDomain.of_group(group.id).domain_id,
            platform_userid=USER_ALT,
            person_uid=USER_MAIN,
        )

        for instance_key in (INSTANCE_A, INSTANCE_B):
            scope = await resolve_identity_scope(_StubChannel(instance_key), USER_ALT)
            assert scope.person_uid == USER_MAIN
            assert scope.affection_person_uid == USER_MAIN

    async def test_person_is_resolved_in_the_affection_domain_when_it_differs(self, scope_db: None) -> None:
        """好感域被覆盖时，好感那一侧的人必须在**好感域**里解析，不能套用记忆域的结果"""
        memory_group = await _make_group("mem")
        affection_group = await _make_group("aff")
        await _bind(INSTANCE_A, memory_group_id=memory_group.id, affection_group_id=affection_group.id)
        await DBIdentityAlias.create(
            domain_id=ScopeDomain.of_group(affection_group.id).domain_id,
            platform_userid=USER_ALT,
            person_uid=USER_MAIN,
        )

        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_ALT)
        assert scope.person_uid == USER_ALT  # 记忆域里没有这条别名
        assert scope.affection_person_uid == USER_MAIN
        assert scope.affection_columns()["platform_userid"] == USER_MAIN


class TestAliasMergeAndUndo:
    """合并必须用别名指向、删别名即撤销"""

    async def test_merge_redirects_the_key_without_moving_any_row(self, scope_db: None) -> None:
        channel = _StubChannel(INSTANCE_A)
        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBAffection.create(**{**domain.as_columns(), "platform_userid": USER_ALT}, score=5)
        await DBAffection.create(**{**domain.as_columns(), "platform_userid": USER_MAIN}, score=80)

        alias = await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid=USER_ALT,
            person_uid=USER_MAIN,
        )
        merged = await resolve_identity_scope(channel, USER_ALT)
        assert merged.affection_columns()["platform_userid"] == USER_MAIN
        assert merged.is_alias is True
        # 副号自己那一行**没有被搬走**，撤销才可能是无损的
        assert await DBAffection.filter(**domain.as_columns(), platform_userid=USER_ALT).count() == 1

        await alias.delete()
        undone = await resolve_identity_scope(channel, USER_ALT)
        assert undone.affection_columns()["platform_userid"] == USER_ALT
        assert undone.is_alias is False

    async def test_the_original_account_is_kept_for_the_event_log(self, scope_db: None) -> None:
        """流水要能说清"这条是哪个号干的"，所以原始账号一直带在解析结果里"""
        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_ALT)
        assert scope.platform_userid == USER_ALT
        assert scope.affection_person_uid == USER_MAIN

    async def test_one_account_points_at_one_person_only(self, scope_db: None) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        with pytest.raises(Exception):  # noqa: B017 - 只关心唯一约束挡住了第二行
            await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=OTHER_USER)

    async def test_alias_chains_are_not_followed(self, scope_db: None) -> None:
        """链式别名只解析一跳"""
        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_MAIN, person_uid=OTHER_USER)

        resolved = await resolve_person_uids(domain, [USER_ALT])
        assert resolved[USER_ALT] == USER_MAIN  # 只跟一跳，不会变成 OTHER_USER


class TestAliasChainsAreActuallyReported:
    """链式别名必须**留下告警**，且在热路径的形态下就能发现

    这组用例的存在理由是一个具体的坑：原本的判据只拿"本批查到的那些行"互相比对，
    于是只有当别名与它的目标**恰好同批**时才发现得了链——而热路径上一次只解析一个人，
    等于永远不告警。链式指向意味着一次错误合并的影响范围会随时间悄悄扩大，
    不告警就没人会发现这条不变式已经破了。

    **不能用 `caplog`**：产品代码走 loguru，它不进 stdlib logging，`caplog.records` 恒为空，
    拿它断言"日志里有某句话"是一条永远为真的空断言（同一个坑见
    `tests/test_deployment_scope_isolation.py`）。
    """

    @staticmethod
    def _capture() -> Tuple[List[str], int]:
        from loguru import logger

        captured: List[str] = []
        sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
        return captured, sink_id

    async def test_a_chain_is_reported_when_resolving_a_single_account(self, scope_db: None) -> None:
        """热路径形态：一次只解析一个人，链仍然必须被报出来"""
        from loguru import logger

        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_MAIN, person_uid=OTHER_USER)

        captured, sink_id = self._capture()
        try:
            resolved = await resolve_person_uids(domain, [USER_ALT])
        finally:
            logger.remove(sink_id)

        assert resolved[USER_ALT] == USER_MAIN
        chain_warnings = [line for line in captured if "链式指向" in line]
        assert len(chain_warnings) == 1
        assert USER_MAIN in chain_warnings[0]

    async def test_a_healthy_alias_is_not_reported(self, scope_db: None) -> None:
        """没有链的普通合并不能告警，否则真正的链会淹在噪声里"""
        from loguru import logger

        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)

        captured, sink_id = self._capture()
        try:
            await resolve_person_uids(domain, [USER_ALT])
        finally:
            logger.remove(sink_id)
        assert [line for line in captured if "链式指向" in line] == []

    async def test_a_self_pointing_alias_is_not_reported_as_a_chain(self, scope_db: None) -> None:
        """指向自己的退化行不是链，报出来只会让真正的链淹在噪声里"""
        from loguru import logger

        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_ALT)

        captured, sink_id = self._capture()
        try:
            resolved = await resolve_person_uids(domain, [USER_ALT])
        finally:
            logger.remove(sink_id)
        assert resolved[USER_ALT] == USER_ALT
        assert [line for line in captured if "链式指向" in line] == []

    async def test_a_chain_in_another_domain_is_not_reported(self, scope_db: None) -> None:
        """判链也必须按域来——别的域里的同号是另一个人，不是这条链的下一跳"""
        from loguru import logger

        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        other = ScopeDomain.of_instance(ADAPTER, INSTANCE_B)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        await DBIdentityAlias.create(domain_id=other.domain_id, platform_userid=USER_MAIN, person_uid=OTHER_USER)

        captured, sink_id = self._capture()
        try:
            await resolve_person_uids(domain, [USER_ALT])
        finally:
            logger.remove(sink_id)
        assert [line for line in captured if "链式指向" in line] == []

    async def test_batch_resolution_covers_every_input(self, scope_db: None) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        await DBIdentityAlias.create(domain_id=domain.domain_id, platform_userid=USER_ALT, person_uid=USER_MAIN)
        resolved = await resolve_person_uids(domain, [USER_ALT, OTHER_USER, USER_ALT])
        assert resolved == {USER_ALT: USER_MAIN, OTHER_USER: OTHER_USER}

    async def test_batch_scope_resolution_covers_every_input(self, scope_db: None) -> None:
        scopes = await resolve_identity_scopes(_StubChannel(INSTANCE_A), [USER_MAIN, OTHER_USER, USER_MAIN])
        assert set(scopes) == {USER_MAIN, OTHER_USER}
        assert all(isinstance(scope, IdentityScope) for scope in scopes.values())


class TestResolutionIsNotCached:
    """改域 / 合并账号必须**热生效**

    这组用例是"刻意不做缓存"这条取舍的守卫：给解析器加任何 TTL 缓存都会让它们变红。
    """

    async def test_joining_a_group_takes_effect_on_the_next_resolution(self, scope_db: None) -> None:
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_leaving_a_group_takes_effect_on_the_next_resolution(self, scope_db: None) -> None:
        group = await _make_group()
        binding = await _bind(INSTANCE_A, memory_group_id=group.id)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)
        await binding.delete()
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)

    async def test_alias_creation_and_removal_take_effect_immediately(self, scope_db: None) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        assert await resolve_person_uid(domain, USER_ALT) == USER_ALT
        alias = await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid=USER_ALT,
            person_uid=USER_MAIN,
        )
        assert await resolve_person_uid(domain, USER_ALT) == USER_MAIN
        await alias.delete()
        assert await resolve_person_uid(domain, USER_ALT) == USER_ALT

    async def test_memory_allowed_keys_take_effect_immediately(self, scope_db: None) -> None:
        assert await resolve_domain_instance_keys(ADAPTER, INSTANCE_A) == resolve_allowed_instance_keys(INSTANCE_A)
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)
        allowed = await resolve_domain_instance_keys(ADAPTER, INSTANCE_A)
        assert allowed is not None
        assert INSTANCE_B in allowed


class TestCrossTenantDomainGuard:
    """跨租户串域：把别的租户的实例拉进自己的组必须被挡住

    接口层另有 `tenant_perm.assert_instance_accessible`（越权 404），
    这里守的是"从别的入口进来的同一个动作"——两扇门缺一不可，理由与 #51 相同。
    """

    @staticmethod
    async def _instance(instance_key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
        return await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            tenant_id=tenant_id,
        )

    async def test_same_tenant_instance_is_joinable(self, scope_db: None) -> None:
        group = await _make_group(tenant_id=1)
        await self._instance(INSTANCE_A, tenant_id=1)
        await DBInstanceGroup.assert_instance_joinable(group, ADAPTER, INSTANCE_A)

    async def test_platform_group_accepts_platform_instance(self, scope_db: None) -> None:
        group = await _make_group(tenant_id=None)
        await self._instance(INSTANCE_A, tenant_id=None)
        await DBInstanceGroup.assert_instance_joinable(group, ADAPTER, INSTANCE_A)

    @pytest.mark.parametrize(
        ("group_tenant", "instance_tenant"),
        [(1, 2), (1, None), (None, 1)],
    )
    async def test_mismatched_ownership_is_rejected(
        self,
        scope_db: None,
        group_tenant: Optional[int],
        instance_tenant: Optional[int],
    ) -> None:
        group = await _make_group(tenant_id=group_tenant)
        await self._instance(INSTANCE_A, tenant_id=instance_tenant)
        with pytest.raises(ValueError, match="归属不一致"):
            await DBInstanceGroup.assert_instance_joinable(group, ADAPTER, INSTANCE_A)

    async def test_unknown_instance_is_rejected_fail_closed(self, scope_db: None) -> None:
        """实例查不到时**拒绝**：这里放行的代价是一份归属不明的数据被并进某个租户的域"""
        group = await _make_group(tenant_id=1)
        with pytest.raises(ValueError, match="实例不存在"):
            await DBInstanceGroup.assert_instance_joinable(group, ADAPTER, "never_created")

    async def test_the_guard_is_adapter_aware(self, scope_db: None) -> None:
        """同名实例可以存在于两个适配器下，校验必须带上 `adapter_key`"""
        group = await _make_group(tenant_id=1)
        await self._instance(INSTANCE_A, tenant_id=1)
        await DBAdapterInstance.create(adapter_key="discord", instance_key=INSTANCE_A, tenant_id=2)
        await DBInstanceGroup.assert_instance_joinable(group, ADAPTER, INSTANCE_A)
        with pytest.raises(ValueError, match="归属不一致"):
            await DBInstanceGroup.assert_instance_joinable(group, "discord", INSTANCE_A)


class TestTheGuardLivesOnTheWriteEntry:
    """归属校验必须长在**唯一的写入口**上，而不是一个"记得调"的独立函数

    摆成独立函数时，后续任何一处 `DBInstanceDomainBinding.create(...)` 都能绕开它，
    而绕开的代价是一个租户的实例被放进另一个租户的域。判据与 `DBChatChannel.set_preset`
    把可见性校验放在模型层是同一条。
    """

    @staticmethod
    async def _instance(instance_key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
        return await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=instance_key, tenant_id=tenant_id)

    async def test_write_entry_rejects_a_foreign_instance(self, scope_db: None) -> None:
        group = await _make_group(tenant_id=1)
        await self._instance(INSTANCE_A, tenant_id=2)
        with pytest.raises(ValueError, match="归属不一致"):
            await DBInstanceDomainBinding.bind_to_groups(ADAPTER, INSTANCE_A, memory_group=group)
        assert await DBInstanceDomainBinding.filter(instance_key=INSTANCE_A).count() == 0
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)

    async def test_write_entry_also_guards_the_affection_override(self, scope_db: None) -> None:
        """好感域覆盖同样要校验——只校验记忆组等于把好感这一侧敞开

        `assert_instance_joinable` 单独用时够不着这一半：它一次只知道一个组。
        """
        own = await _make_group("own", tenant_id=1)
        foreign = await _make_group("foreign", tenant_id=2)
        await self._instance(INSTANCE_A, tenant_id=1)
        with pytest.raises(ValueError, match="归属不一致"):
            await DBInstanceDomainBinding.bind_to_groups(
                ADAPTER,
                INSTANCE_A,
                memory_group=own,
                affection_group=foreign,
            )
        assert await DBInstanceDomainBinding.filter(instance_key=INSTANCE_A).count() == 0

    async def test_write_entry_accepts_a_matching_instance_and_takes_effect(self, scope_db: None) -> None:
        group = await _make_group(tenant_id=1)
        await self._instance(INSTANCE_A, tenant_id=1)
        await DBInstanceDomainBinding.bind_to_groups(ADAPTER, INSTANCE_A, memory_group=group)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_rebinding_updates_the_same_row(self, scope_db: None) -> None:
        """重复绑定必须改同一行，否则唯一约束会把第二次操作变成一次报错"""
        first = await _make_group("first", tenant_id=1)
        second = await _make_group("second", tenant_id=1)
        await self._instance(INSTANCE_A, tenant_id=1)
        await DBInstanceDomainBinding.bind_to_groups(ADAPTER, INSTANCE_A, memory_group=first)
        await DBInstanceDomainBinding.bind_to_groups(ADAPTER, INSTANCE_A, memory_group=second)
        assert await DBInstanceDomainBinding.filter(instance_key=INSTANCE_A).count() == 1
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(second.id)

    async def test_unbinding_removes_the_row_entirely(self, scope_db: None) -> None:
        """两个组都取消 = 回到自成一域 = 没有行（零配置的标准形态）"""
        group = await _make_group(tenant_id=1)
        await self._instance(INSTANCE_A, tenant_id=1)
        await DBInstanceDomainBinding.bind_to_groups(ADAPTER, INSTANCE_A, memory_group=group)
        assert await DBInstanceDomainBinding.bind_to_groups(ADAPTER, INSTANCE_A) is None
        assert await DBInstanceDomainBinding.filter(instance_key=INSTANCE_A).count() == 0
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)


class TestOwnershipChangeReleasesTheDomain:
    """**归属校验只在加入的那一刻成立，之后没有任何东西复查**

    于是有两条不经过任何守卫的跨租户串域路径，两条都必须在写侧把绑定行作废：

    1. 平台方把实例在租户之间划转 —— 实例换了主人却还留在原租户的组里；
    2. 实例被彻底删除后绑定行残留 —— 之后**同名** slug 被重新认领，新实例继承旧域，
       连带读到挂在那个域上的好感行（好感不随实例删除而删除，它挂在域上）。

    读侧刻意不复查（每条消息多查两张表的代价见 `services/identity_scope.py` 的缓存取舍），
    所以这两处是仅有的两道门。
    """

    async def test_release_puts_an_instance_back_into_its_own_domain(self, scope_db: None) -> None:
        group = await _make_group(tenant_id=1)
        await _bind(INSTANCE_A, memory_group_id=group.id)
        assert await DBInstanceDomainBinding.release_instances([INSTANCE_A], reason="用例") == 1
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)

    async def test_release_leaves_other_instances_alone(self, scope_db: None) -> None:
        group = await _make_group(tenant_id=1)
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)
        await DBInstanceDomainBinding.release_instances([INSTANCE_A], reason="用例")
        assert await resolve_memory_domain(ADAPTER, INSTANCE_B) == ScopeDomain.of_group(group.id)

    async def test_transferring_an_instance_between_tenants_drops_its_domain(self, scope_db: None) -> None:
        """走真的接口函数，不是复刻一遍它的逻辑——复刻只能证明我抄对了"""
        from nekro_agent.models.db_tenant import DBTenant
        from nekro_agent.routers.console.tenants import bind_tenant_instances
        from nekro_agent.schemas.tenant import TenantInstanceBindRequest

        owner = DBUser(id=1, username="platform", role=Role.Super.value)
        tenant_a = await DBTenant.create(name="A")
        tenant_b = await DBTenant.create(name="B")
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, tenant_id=tenant_a.id)
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE_B, tenant_id=tenant_a.id)
        group = await _make_group(tenant_id=tenant_a.id)
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await _bind(INSTANCE_B, memory_group_id=group.id)
        # 租户 A 的私密好感就挂在这个组域上
        await DBAffection.create(**ScopeDomain.of_group(group.id).as_columns(), platform_userid=USER_MAIN, score=88)

        # 平台方先把 INSTANCE_B 从 A 摘下来（覆盖语义：不在列表里的被解绑），再划给 B
        await bind_tenant_instances(tenant_a.id, TenantInstanceBindRequest(instance_keys=[INSTANCE_A]), owner)
        await bind_tenant_instances(tenant_b.id, TenantInstanceBindRequest(instance_keys=[INSTANCE_B]), owner)

        assert await resolve_memory_domain(ADAPTER, INSTANCE_B) == ScopeDomain.of_instance(ADAPTER, INSTANCE_B)
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_B), USER_MAIN)
        assert await DBAffection.filter(**scope.affection_columns()).count() == 0
        # 留在原租户的那个实例不受影响，否则这条修复就变成"改租户会顺手清掉别人的域"
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_replaying_the_same_binding_keeps_the_domain(self, scope_db: None) -> None:
        """接口是幂等覆盖语义：原样重放一次不该把域配置一并抹掉"""
        from nekro_agent.models.db_tenant import DBTenant
        from nekro_agent.routers.console.tenants import bind_tenant_instances
        from nekro_agent.schemas.tenant import TenantInstanceBindRequest

        owner = DBUser(id=1, username="platform", role=Role.Super.value)
        tenant = await DBTenant.create(name="A")
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, tenant_id=tenant.id)
        group = await _make_group(tenant_id=tenant.id)
        await _bind(INSTANCE_A, memory_group_id=group.id)

        await bind_tenant_instances(tenant.id, TenantInstanceBindRequest(instance_keys=[INSTANCE_A]), owner)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)

    async def test_purging_an_instance_drops_its_domain_binding(self, scope_db: None) -> None:
        """否则同名新实例（可能属于另一个租户）直接继承旧域并读到旧租户的好感"""
        from nekro_agent.services.console import instance_manager

        instance = await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, tenant_id=1)
        group = await _make_group(tenant_id=1)
        await _bind(INSTANCE_A, memory_group_id=group.id)
        await DBAffection.create(**ScopeDomain.of_group(group.id).as_columns(), platform_userid=USER_MAIN, score=99)

        async def _no_vectors(_instance_key: str) -> None:
            """向量库不参与本契约；它不可用时产品代码本来就只是告警"""

        original = instance_manager.purge_instance_vectors
        instance_manager.purge_instance_vectors = _no_vectors  # type: ignore[assignment]
        try:
            counts = await instance_manager.purge_instance(instance)
        finally:
            instance_manager.purge_instance_vectors = original  # type: ignore[assignment]

        assert counts["instance_domain_binding"] == 1
        assert await DBInstanceDomainBinding.filter(instance_key=INSTANCE_A).count() == 0

        # 另一个租户认领同一个 slug，必须落在自成一域、读不到那 99 分
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE_A, tenant_id=2)
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        assert scope.domain == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        assert await DBAffection.filter(**scope.affection_columns()).count() == 0


class TestDirtyGroupColumnsDoNotKillTheHotPath:
    """绑定列里的非法组 ID 是**数据**，不是调用方的 bug

    `ScopeDomain.of_group` 对非正值抛异常是为了挡住调用方拼错键；照搬到读侧就变成
    "库里一个 0 让这个实例的每一条消息都炸在解析器里"，整个 Bot 当场失声。
    回落到自成一域是安全的那一侧——它等于今天的行为。
    """

    @pytest.mark.parametrize("dirty", [0, -1])
    async def test_a_non_positive_memory_group_falls_back_to_the_self_domain(self, scope_db: None, dirty: int) -> None:
        await _bind(INSTANCE_A, memory_group_id=dirty)
        assert await resolve_memory_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        assert scope.affection_columns()["domain_group_id"] == SELF_DOMAIN_GROUP_ID

    async def test_a_non_positive_affection_group_falls_back_to_the_memory_domain(self, scope_db: None) -> None:
        """好感覆盖列脏掉时回落到记忆域，而不是连记忆域一起丢掉"""
        group = await _make_group()
        await _bind(INSTANCE_A, memory_group_id=group.id, affection_group_id=0)
        assert await resolve_affection_domain(ADAPTER, INSTANCE_A) == ScopeDomain.of_group(group.id)


class TestPresetDimension:
    """人设维走的是 `get_preset()` 那条回退链，无人设档定死为 0

    用真的 `DBChatChannel` 而不是替身：这条回退链有四层（频道 → 实例默认 → 系统默认 → 内置），
    只测替身等于什么都没测。
    """

    @staticmethod
    async def _channel(instance_key: str = INSTANCE_A, preset_id: Optional[int] = None) -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id="group_777",
            channel_type=ChatType.GROUP.value,
            chat_key=f"{ADAPTER}-{instance_key}-group_777",
            preset_id=preset_id,
            data="{}",
        )

    @staticmethod
    async def _preset(name: str) -> DBPreset:
        return await DBPreset.create(
            name=name,
            title=name,
            avatar="",
            content="内容",
            description="",
            tags="",
            author="test",
        )

    async def test_unbound_channel_lands_on_preset_none(self, scope_db: None) -> None:
        channel = await self._channel()
        assert await channel.get_effective_preset_id() == PRESET_NONE == 0

    async def test_bound_channel_uses_its_preset_id(self, scope_db: None) -> None:
        preset = await self._preset("甲")
        channel = await self._channel(preset_id=preset.id)
        assert await channel.get_effective_preset_id() == preset.id

    async def test_instance_default_preset_counts_as_the_effective_persona(self, scope_db: None) -> None:
        """频道没绑但实例配了默认人设时，说话的就是那个人格，隔离键必须跟着它

        只读 `channel.preset_id` 的话，"把实例默认人设显式绑到频道上"这个纯运维动作
        会把该频道的数据整体切走一份，而运营完全没有换人设的意图。
        """
        preset = await self._preset("实例默认")
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_A,
            metadata_json=f'{{"{INSTANCE_METADATA_DEFAULT_PRESET_ID}": {preset.id}}}',
        )
        channel = await self._channel()
        bound = await self._channel(instance_key=INSTANCE_B)
        bound.preset_id = preset.id
        await bound.save()

        assert await channel.get_effective_preset_id() == preset.id
        assert await bound.get_effective_preset_id() == preset.id

    async def test_system_default_preset_is_honoured(self, scope_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config

        preset = await self._preset("系统默认")
        monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", preset.id)
        channel = await self._channel()
        assert await channel.get_effective_preset_id() == preset.id

    async def test_deleted_preset_falls_through_and_leaves_orphans(self, scope_db: None) -> None:
        """人设删掉后新数据落到下一档，旧数据留成孤儿（不级联删）"""
        preset = await self._preset("会被删掉")
        channel = await self._channel(preset_id=preset.id)
        assert await channel.get_effective_preset_id() == preset.id

        await DBAffection.create(
            **ScopeDomain.of_instance(ADAPTER, INSTANCE_A).as_columns(),
            platform_userid=USER_MAIN,
            preset_id=preset.id,
            score=50,
        )
        await preset.delete()

        assert await channel.get_effective_preset_id() == PRESET_NONE
        orphan = await DBAffection.filter(preset_id=preset.id).first()
        assert orphan is not None
        assert orphan.score == 50

    async def test_editing_a_preset_is_not_switching_it(self, scope_db: None) -> None:
        """判据是 preset 行的 ID，不是内容 hash——改文案不该让所有人的好感归零"""
        preset = await self._preset("甲")
        channel = await self._channel(preset_id=preset.id)
        before = await channel.get_effective_preset_id()
        preset.content = "完全不同的内容"
        await preset.save()
        assert await channel.get_effective_preset_id() == before

    async def test_two_presets_are_two_sets_of_data(self, scope_db: None) -> None:
        """同一个人在两个人设下是两份好感——同一域、同一人、不同人设，键必须不同"""
        first = await self._preset("甲")
        second = await self._preset("乙")
        channel = await self._channel(preset_id=first.id)

        scope_first = await resolve_identity_scope(channel, USER_MAIN)
        channel.preset_id = second.id
        await channel.save()
        scope_second = await resolve_identity_scope(channel, USER_MAIN)

        assert scope_first.affection_columns() != scope_second.affection_columns()
        assert scope_first.domain == scope_second.domain
        assert scope_first.affection_person_uid == scope_second.affection_person_uid

    async def test_caller_supplied_preset_id_is_used_as_is(self, scope_db: None) -> None:
        """热路径可以把已经取好的人设标识传进来，省掉最多三次点查"""
        channel = _StubChannel(INSTANCE_A, preset_id=11)
        scope = await resolve_identity_scope(channel, USER_MAIN, preset_id=42)
        assert scope.preset_id == 42

    @pytest.mark.parametrize("supplied", [0, -1])
    async def test_non_positive_preset_id_collapses_to_preset_none(self, scope_db: None, supplied: int) -> None:
        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN, preset_id=supplied)
        assert scope.preset_id == PRESET_NONE


class TestChatKeyEntryPoint:
    """只有 `chat_key` 的调用方走这个入口，定位不到频道时**拒绝**而不是回落"""

    async def test_resolves_through_the_channel_row(self, scope_db: None) -> None:
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_A,
            channel_id="group_777",
            channel_type=ChatType.GROUP.value,
            chat_key=f"{ADAPTER}-{INSTANCE_A}-group_777",
            data="{}",
        )
        scope = await resolve_identity_scope_by_chat_key(f"{ADAPTER}-{INSTANCE_A}-group_777", USER_MAIN)
        assert scope is not None
        assert scope.domain == ScopeDomain.of_instance(ADAPTER, INSTANCE_A)

    async def test_unknown_chat_key_returns_none(self, scope_db: None) -> None:
        """算不出来就当作全局"正是要堵的那个泄露形态，因此这里返回 None 而不是某个默认域"""
        assert await resolve_identity_scope_by_chat_key("onebot_v11-nope-group_1", USER_MAIN) is None


class TestTheResolverIsTheOnlyKeyBuilder:
    """键的两种编码只有一个产生者：写进去的行，用解析出来的键必须查得到"""

    @pytest.mark.parametrize(
        "binding_kwargs",
        [
            {},
            {"memory_group_id": 1},
            {"memory_group_id": 1, "affection_group_id": 2},
            {"affection_group_id": 2},
        ],
    )
    async def test_write_then_read_round_trips_in_every_domain_shape(
        self,
        scope_db: None,
        binding_kwargs: Dict[str, Any],
    ) -> None:
        for _ in range(2):
            await _make_group()
        if binding_kwargs:
            await _bind(INSTANCE_A, **binding_kwargs)

        scope = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        await DBAffection.create(**scope.affection_columns(), score=7)

        again = await resolve_identity_scope(_StubChannel(INSTANCE_A), USER_MAIN)
        record = await DBAffection.filter(**again.affection_columns()).first()
        assert record is not None
        assert record.score == 7

    async def test_different_domains_never_read_each_other(self, scope_db: None) -> None:
        rows: List[Tuple[str, int]] = [(INSTANCE_A, 11), (INSTANCE_B, 22)]
        for instance_key, score in rows:
            scope = await resolve_identity_scope(_StubChannel(instance_key), USER_MAIN)
            await DBAffection.create(**scope.affection_columns(), score=score)

        for instance_key, score in rows:
            scope = await resolve_identity_scope(_StubChannel(instance_key), USER_MAIN)
            found = await DBAffection.filter(**scope.affection_columns()).all()
            assert [record.score for record in found] == [score]
