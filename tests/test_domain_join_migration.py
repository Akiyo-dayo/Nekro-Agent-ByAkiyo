"""实例入域并入存量好感的行为契约

## 这条测试要防的是什么

`set_instance_domain` 只写绑定行时，实例「自成一域」时期的全部好感行
（`domain_group_id=0` + 本行 `(adapter_key, instance_key)`）在入组那一刻起就再也
查不到——键换了。这正是 `db_affection.py`「已知代价」注释点名的坑：运营把一个
跑了一阵子的实例拉进实例组，该实例所有用户的好感**在运行时当场归零**。

`migrate_affection` 选项把存量行并入目标组域。本文件钉三件事：

1. **搬的是哪一堆**：只有「自成一域时期」的行（`adapter_key/instance_key` 逐字是本实例、
   `domain_group_id=0`）。组域里的行是共享历史，组→组、组→自成一域都没有并入；
2. **撞上行怎么折**：目标域已有同（人, 人设）行时按「取高」折叠——差额记一笔
   `domain_merge` 补偿事件（可审计），叙事取分数更高一侧，互动次数相加；
3. **并入与绑定同一事务**：并入选项在「回到自成一域」「已在组域」两个方向上被拒，
   且不留下半成品。

预览与执行同一份判据（`preview_domain_join` / `merge_self_domain_into_group`），
与账号合并的「先预览后执行」同形。
"""

import datetime
import itertools
from typing import List, Optional

import pytest
from tortoise import Tortoise
from tortoise.queryset import QuerySet

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_identity import ALIAS_SOURCE_MANUAL, DBIdentityAlias, DBIdentityPerson
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.identity import (
    InstanceDomainUpdateRequest,
    get_domain_join_preview,
    set_instance_domain,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.merge import merge_accounts, unmerge_account
from nekro_agent.services.affection.stage import AffectionEventSource, stage_of
from nekro_agent.services.affection.store import apply_delta, invalidate_cache, load_snapshot
from nekro_agent.services.identity_admin import merge_self_domain_into_group
from nekro_agent.services.identity_scope import ScopeDomain, resolve_memory_domain, resolve_person_uid
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"

# 与测试环境同形：acme 名下 inst1，另一租户名下 rival1
INSTANCE_LAYOUT = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, RIVAL_TENANT_ID),
]


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=ACME_TENANT_ID, name="acme", display_name="Acme")
        await DBTenant.create(id=RIVAL_TENANT_ID, name="rival", display_name="Rival")
        for instance_key, tenant_id in INSTANCE_LAYOUT:
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；作用域判定只读这几个字段"""
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


async def make_group(name: str, tenant_id: Optional[int]) -> DBInstanceGroup:
    return await DBInstanceGroup.create(name=name, note="", tenant_id=tenant_id)


async def seed_affection(
    domain: ScopeDomain,
    *,
    platform_userid: str,
    preset_id: int,
    score: int,
    events: int = 0,
    interaction_count: int = 0,
) -> DBAffection:
    """在指定的 (域, 人, 人设) 上建一行好感，并按需补几条流水"""
    record = await DBAffection.create(
        **domain.as_columns(),
        platform_userid=platform_userid,
        preset_id=preset_id,
        nickname=f"nick-{platform_userid}",
        score=score,
        stage=stage_of(score).value,
        state_text=f"state-{score}",
        last_event_text=f"last-{score}",
        interaction_count=interaction_count,
    )
    for index in range(events):
        await DBAffectionEvent.create(
            **domain.as_columns(),
            platform_userid=platform_userid,
            preset_id=preset_id,
            source_platform_userid=platform_userid,
            delta=1,
            score_after=score,
            source="llm",
            reason=f"event-{index}",
        )
    return record


def self_domain() -> ScopeDomain:
    return ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)


async def self_domain_rows() -> List[DBAffection]:
    return await DBAffection.filter(**self_domain().as_columns()).order_by("id")


async def group_domain_rows(group_id: int) -> List[DBAffection]:
    return await DBAffection.filter(**ScopeDomain.of_group(group_id).as_columns()).order_by("id")


class TestMigrationRequiresTheRightDirection:
    """「并入」只在「自成一域 → 组域」这一个方向上合法"""

    async def test_migrate_flag_rejected_when_returning_to_self_domain(self, db) -> None:
        """回到自成一域没有「并入」可言——目标域是空的，勾了等于一个静默不生效的选项"""
        with pytest.raises(ValidationError):
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=None, migrate_affection=True),
                _current_user=acme_admin(),
            )

    async def test_migrate_flag_rejected_when_already_in_a_group(self, db) -> None:
        """组 → 组不并入：源域里的行是上一个组的共享历史，随实例搬走会带走别家成员的数据"""
        first = await make_group("g-first", ACME_TENANT_ID)
        second = await make_group("g-second", ACME_TENANT_ID)
        await DBInstanceDomainBinding.bind_to_groups(ADAPTER, OWN_INSTANCE, memory_group=first)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=10)

        with pytest.raises(ValidationError):
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=second.id, migrate_affection=True),
                _current_user=acme_admin(),
            )

        # 绑定与存量都原样：拒绝不能留下半成品
        binding = await DBInstanceDomainBinding.get_binding(ADAPTER, OWN_INSTANCE)
        assert binding is not None and binding.memory_group_id == first.id
        assert len(await self_domain_rows()) == 1
        assert await group_domain_rows(second.id) == []

    async def test_binding_failure_rolls_back_nothing_was_migrated(self, db) -> None:
        """并入与绑定同一事务：模型层拒了绑定时，源域的行一行都不能动

        平台方把 acme 的实例往 rival 的组里放，两道接口门都过得去（他看得见全部），
        只有 `bind_to_groups` 的归属校验会拒——这条用例因此按 400 而不是 404。
        """
        rival_group = await make_group("rival-family", RIVAL_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=42)

        with pytest.raises(ValidationError):
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=rival_group.id, migrate_affection=True),
                _current_user=platform_owner(),
            )

        assert await DBInstanceDomainBinding.filter(instance_key=OWN_INSTANCE).count() == 0
        rows = await self_domain_rows()
        assert len(rows) == 1 and rows[0].score == 42


class TestSelfDomainRowsFollowTheInstance:
    """并入的本体：行、流水、别名、档案一起换域"""

    async def test_rows_events_aliases_and_persons_move_to_the_group_domain(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=42, events=2, interaction_count=7)
        await seed_affection(self_domain(), platform_userid="u2", preset_id=3, score=9)
        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid="u1-old",
            person_uid="u1",
            source=ALIAS_SOURCE_MANUAL,
            operator="ops",
        )
        await DBIdentityPerson.create(domain_id=self_domain().domain_id, person_uid="u1", display_name="甲")

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        target = ScopeDomain.of_group(group.id)
        rows = await group_domain_rows(group.id)
        assert {(row.platform_userid, row.preset_id): row.score for row in rows} == {
            ("u1", 0): 42,
            ("u2", 3): 9,
        }
        # 域三列真的换了：不是"留着旧键复制一份"，是同一个行换了键
        assert all(row.adapter_key == "" and row.instance_key == "" for row in rows)
        assert await self_domain_rows() == []

        assert await DBAffectionEvent.filter(**target.as_columns()).count() == 2
        assert await DBAffectionEvent.filter(**self_domain().as_columns()).count() == 0

        alias = await DBIdentityAlias.filter(platform_userid="u1-old").first()
        assert alias is not None and alias.domain_id == target.domain_id
        person = await DBIdentityPerson.filter(person_uid="u1").first()
        assert person is not None and person.domain_id == target.domain_id

        # 响应里如实回显实做结果
        assert response.migration is not None
        assert response.migration.affection_rows == 2
        assert response.migration.folded_rows == 0
        assert response.migration.event_rows == 2
        assert response.migration.alias_rows == 1
        assert response.migration.person_rows == 1
        assert response.group_id == group.id

    async def test_join_without_the_flag_leaves_rows_behind(self, db) -> None:
        """默认行为逐字不变：不勾选 = 只改归属，存量留在旧键下（与改造前相同）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=42)

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id),
            _current_user=acme_admin(),
        )

        assert response.migration is None
        assert len(await self_domain_rows()) == 1
        assert await group_domain_rows(group.id) == []


class TestFoldedRowsTakeTheHigherScore:
    """同（人, 人设）撞上目标域已有行时的折叠规则"""

    async def test_higher_self_domain_score_wins_and_writes_a_compensation_event(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(
            self_domain(),
            platform_userid="u1",
            preset_id=2,
            score=80,
            interaction_count=5,
        )
        await seed_affection(
            target,
            platform_userid="u1",
            preset_id=2,
            score=30,
            interaction_count=3,
        )

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        rows = await group_domain_rows(group.id)
        assert len(rows) == 1
        twin = rows[0]
        # 分数取高、叙事取更高一侧、互动相加
        assert twin.score == 80
        assert twin.state_text == "state-80"
        assert twin.last_event_text == "last-80"
        assert twin.interaction_count == 8
        assert twin.stage == "close"  # stage_of(80)

        # 差额记一笔 domain_merge 补偿事件，挂在目标域、可审计
        events = await DBAffectionEvent.filter(**target.as_columns(), source=AffectionEventSource.DOMAIN_MERGE.value)
        assert len(events) == 1
        assert events[0].delta == 50 and events[0].score_after == 80
        assert events[0].platform_userid == "u1" and events[0].preset_id == 2

        assert response.migration is not None
        assert response.migration.folded_rows == 1
        assert response.migration.score_delta == 50
        assert response.migration.affection_rows == 0

    async def test_lower_self_domain_score_keeps_the_target_row(self, db) -> None:
        """目标域更高的那一份不被拉低；折叠仍然发生（源行删除、互动相加）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=10, interaction_count=2)
        await seed_affection(target, platform_userid="u1", preset_id=0, score=60, interaction_count=9)

        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        rows = await group_domain_rows(group.id)
        assert len(rows) == 1
        assert rows[0].score == 60
        assert rows[0].state_text == "state-60"
        assert rows[0].interaction_count == 11
        # 没有差额就不该有补偿事件
        assert (
            await DBAffectionEvent.filter(**target.as_columns(), source=AffectionEventSource.DOMAIN_MERGE.value).count()
            == 0
        )


class TestAliasConflictsStayWithTheTargetDomain:
    """目标域已有的账号指向不被一次入域改写"""

    async def test_conflicting_alias_is_not_moved_and_is_reported(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        # 同一个账号在两个域里指向不同的人：目标域说了算，源域那条留在原地
        await DBIdentityAlias.create(
            domain_id=target.domain_id,
            platform_userid="u9",
            person_uid="u9-main-in-group",
            source=ALIAS_SOURCE_MANUAL,
        )
        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid="u9",
            person_uid="u9-other",
            source=ALIAS_SOURCE_MANUAL,
        )
        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid="u8",
            person_uid="u8-main",
            source=ALIAS_SOURCE_MANUAL,
        )

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        conflicted = await DBIdentityAlias.filter(domain_id=self_domain().domain_id, platform_userid="u9").first()
        assert conflicted is not None and conflicted.person_uid == "u9-other"
        kept = await DBIdentityAlias.filter(domain_id=target.domain_id, platform_userid="u9").first()
        assert kept is not None and kept.person_uid == "u9-main-in-group"
        moved = await DBIdentityAlias.filter(domain_id=target.domain_id, platform_userid="u8").first()
        assert moved is not None and moved.person_uid == "u8-main"

        assert response.migration is not None
        assert response.migration.alias_rows == 1
        assert response.migration.alias_conflicts == ["u9"]


async def target_aliases(group_id: int) -> dict:
    """目标域里现在全部别名：账号 -> 人"""
    domain_id = ScopeDomain.of_group(group_id).domain_id
    return {row.platform_userid: row.person_uid for row in await DBIdentityAlias.filter(domain_id=domain_id)}


async def chain_violations(domain_id: str) -> list:
    """域里「指向的人自己又是别名」的行——别名不成链这条不变式的反例"""
    rows = await DBIdentityAlias.filter(domain_id=domain_id)
    accounts = {row.platform_userid for row in rows}
    return sorted((row.platform_userid, row.person_uid) for row in rows if row.person_uid in accounts)


async def add_alias(domain: ScopeDomain, account: str, person: str) -> None:
    await DBIdentityAlias.create(
        domain_id=domain.domain_id,
        platform_userid=account,
        person_uid=person,
        source=ALIAS_SOURCE_MANUAL,
        operator="ops",
    )


class TestSourceRowsAreNormalisedByTargetAliases:
    """目标域已有 P→Q 时，源域里 P 名下的好感必须落到 Q 上（运行时读的是 Q）

    运行时按 `resolve_person_uid` 把 P 解析成 Q 再读好感行；若源行原样以 P 搬过去，
    那一行就永远读不到——并入「成功」了，数据当场失联。
    """

    async def test_row_folds_into_the_final_person_taking_the_higher_score(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=70, events=2, interaction_count=4)
        await seed_affection(target, platform_userid="Q", preset_id=0, score=30, interaction_count=1)

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        rows = await group_domain_rows(group.id)
        assert [(row.platform_userid, row.preset_id, row.score) for row in rows] == [("Q", 0, 70)]
        assert rows[0].interaction_count == 5
        # 运行时读的就是这一行：P 解析成 Q
        assert await resolve_person_uid(target, "P") == "Q"
        # 事件流水跟着改挂到最终的人名下，补偿事件也在 Q 上
        assert await DBAffectionEvent.filter(**target.as_columns(), platform_userid="P").count() == 0
        assert await DBAffectionEvent.filter(**target.as_columns(), platform_userid="Q").count() == 3
        compensation = await DBAffectionEvent.filter(
            **target.as_columns(),
            source=AffectionEventSource.DOMAIN_MERGE.value,
        )
        assert len(compensation) == 1 and compensation[0].platform_userid == "Q" and compensation[0].delta == 40

        assert response.migration is not None
        assert response.migration.folded_rows == 1
        assert response.migration.affection_rows == 0
        assert response.migration.remapped_rows == 1
        assert response.migration.score_delta == 40

    async def test_row_without_a_twin_moves_but_is_rekeyed_to_the_final_person(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(self_domain(), platform_userid="P", preset_id=3, score=55, events=1)

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        rows = await group_domain_rows(group.id)
        assert [(row.platform_userid, row.preset_id, row.score) for row in rows] == [("Q", 3, 55)]
        assert await DBAffectionEvent.filter(**target.as_columns(), platform_userid="Q").count() == 1
        assert response.migration is not None
        assert response.migration.affection_rows == 1 and response.migration.remapped_rows == 1

    async def test_two_source_rows_that_normalise_to_the_same_person_collapse_into_one(self, db) -> None:
        """源域里 P 与 Q 各有一行、目标域说 P→Q：归一之后撞的是**源行彼此**，不是目标行"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80, interaction_count=2)
        await seed_affection(self_domain(), platform_userid="Q", preset_id=0, score=20, interaction_count=3)

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        rows = await group_domain_rows(group.id)
        assert [(row.platform_userid, row.score, row.interaction_count) for row in rows] == [("Q", 80, 5)]
        assert response.migration is not None
        assert response.migration.affection_rows + response.migration.folded_rows == 2
        assert response.migration.folded_rows == 1

    @pytest.mark.parametrize("target_has_twin", [True, False])
    @pytest.mark.parametrize("order", list(itertools.permutations([("P", 50), ("Q", 30), ("R", 5)])))
    async def test_three_way_collapse_is_independent_of_source_row_order(
        self,
        db,
        order,
        target_has_twin: bool,
    ) -> None:
        """P、Q、R 三条源行都归一到 Q：分数取最高、叙事跟分数、流水账对得上，且与源行顺序无关

        回归：折叠循环里 `twin` 是循环外读出来的内存对象，`apply_delta`
        在库里把分数涨上去之后它仍是旧值，下一条折叠到同一 twin 的源行 `twin.save()` 把旧分写回，
        分数被打回去、叙事却已经换成了低分一侧、事件的 `score_after` 与最终分数对不上。
        """
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await add_alias(target, "R", "Q")
        if target_has_twin:
            await seed_affection(target, platform_userid="Q", preset_id=0, score=10, interaction_count=1)
        for uid, score in order:
            await seed_affection(self_domain(), platform_userid=uid, preset_id=0, score=score, interaction_count=2)

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        rows = await group_domain_rows(group.id)
        assert len(rows) == 1 and rows[0].platform_userid == "Q"
        row = rows[0]
        assert row.score == 50, f"折叠后分数应取最高的 50，实际 {row.score}（源行顺序 {order}）"
        assert row.stage == stage_of(50).value
        # 叙事与分数来自同一段关系：最高分那一侧
        assert (row.state_text, row.last_event_text) == ("state-50", "last-50")
        assert row.interaction_count == 6 + (1 if target_has_twin else 0)

        events = await DBAffectionEvent.filter(
            **target.as_columns(),
            source=AffectionEventSource.DOMAIN_MERGE.value,
        ).order_by("id")
        baseline = 10 if target_has_twin else None
        if baseline is not None:
            assert events, "目标域已有 10 分的行，较高的源行必须记补偿事件"
            # 补偿的增量之和 = 最终分 - 并入前的分；最后一笔的 score_after 就是最终分
            assert sum(event.delta for event in events) == 50 - baseline
            assert events[-1].score_after == 50
            assert response.migration is not None and response.migration.score_delta == 50 - baseline
        # 每条补偿事件的 score_after 必须与「它之前的分 + 它的增量」对得上
        running = baseline
        for event in events:
            if running is not None:
                running += event.delta
                assert event.score_after == running
            else:
                running = event.score_after
        # 最高分的源行排在最前、又没有目标行时它自己就是落地的行，一笔补偿都没有
        assert running in (None, 50)
        snapshot = await load_snapshot(AffectionKey.of_domain(target, person_uid="Q", preset_id=0), 0)
        assert snapshot.score == 50

    async def test_dormant_rows_of_a_source_secondary_account_stay_dormant(self, db) -> None:
        """源域里本来就是副号的账号（A→P）自己名下那行在源域读不到，搬过去也不该被「激活」折进别人

        这里 A 在目标域里同样是别名（A→Z，同账号冲突、以目标域为准）：若把它当「人」按目标域别名
        归一，休眠的 99 分会被折进 Z——一次不可撤销的物理合并，违背「删别名即撤销」。
        """
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "A", "Z")
        await add_alias(self_domain(), "A", "P")
        await seed_affection(self_domain(), platform_userid="A", preset_id=0, score=99)
        await seed_affection(target, platform_userid="Z", preset_id=0, score=10)

        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        by_uid = {row.platform_userid: row.score for row in await group_domain_rows(group.id)}
        assert by_uid == {"A": 99, "Z": 10}, "副号 A 名下的休眠行被折进了 Z，撤销合并后无法还原"
        assert await resolve_person_uid(target, "A") == "Z"

    async def test_unaffected_rows_are_not_rekeyed(self, db) -> None:
        """反向对照：没有目标域别名的账号原样搬，remapped_rows 为 0"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        await add_alias(ScopeDomain.of_group(group.id), "P", "Q")
        await seed_affection(self_domain(), platform_userid="Z", preset_id=0, score=12)

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert [(row.platform_userid, row.score) for row in await group_domain_rows(group.id)] == [("Z", 12)]
        assert response.migration is not None and response.migration.remapped_rows == 0


async def source_merge(person: str, alias: str, *, person_score: int, alias_score: int) -> None:
    """在自成一域里真实走一遍账号合并：别名 alias→person，人的分数被补到不低于副号（补偿事件记在人的行上）"""
    await seed_affection(self_domain(), platform_userid=person, preset_id=0, score=person_score)
    await seed_affection(self_domain(), platform_userid=alias, preset_id=0, score=alias_score)
    await merge_accounts(self_domain(), person_uid=person, alias_platform_userid=alias, operator="ops")


async def join_group(group: DBInstanceGroup) -> None:
    await set_instance_domain(
        OWN_INSTANCE,
        InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
        _current_user=acme_admin(),
    )


async def score_of(group_id: int, person: str) -> int:
    domain = ScopeDomain.of_group(group_id)
    return (await load_snapshot(AffectionKey.of_domain(domain, person_uid=person, preset_id=0), 0)).score


class TestSourceMergeCompensationSurvivesTheJoinOnlyIfItLanded:
    """源域里 A→P 的 `merge` 补偿（P 行 +50）并入后，撤销 A 不能扣掉别人挣的分

    `unmerge` 按 `(source ∈ {merge, unmerge}, source_platform_userid=A)` 把流水求和扣回。
    源域 P 行折叠进目标域一行**更高**的分数时，这 50 点根本没落进目标行（取高吃掉了），
    可补偿事件随行一起改挂过去，仍带着可撤销标记——撤销 A 就把 Q 的 90 扣成 40。
    取舍：只要某个 (人, 人设) 上发生了折叠，源域里挂在它名下的补偿事件整体改记为 `domain_merge`
    （保留来源账号，仍是审计记录，但不再参与撤销求和）——宁可少扣，不能把别人挣的分扣掉。
    没发生折叠、整行搬过去的，分数原样落地，补偿事件保持可撤销。
    """

    async def test_remapped_fold_into_a_higher_target_row_is_not_revocable(self, db) -> None:
        """目标 Q=90（别名 P→Q），源域 P=20、A=70、A→P（补 50 → P=70）：并入后撤销 A，Q 仍是 90"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(target, platform_userid="Q", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)
        assert await score_of(group.id, "Q") == 90
        assert await target_aliases(group.id) == {"P": "Q", "A": "Q"}

        result = await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert result.total_delta == 0
        assert await score_of(group.id, "Q") == 90, "撤销 A 把没落进 Q 的补偿扣回来了：Q 自己挣的分被扣掉"

    async def test_unremapped_fold_into_a_higher_target_row_is_not_revocable(self, db) -> None:
        """同形、没有归一：目标域自己就有 P=90 一行"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)
        assert await score_of(group.id, "P") == 90

        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 90

    async def test_partial_landing_is_never_over_deducted(self, db) -> None:
        """目标 P=60、源域 P 补到 70：折叠只落了 10，撤销 A 不能扣 50（宁可少扣）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=60)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)
        assert await score_of(group.id, "P") == 70

        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 70

    async def test_rows_that_collapse_among_themselves_are_also_not_revocable(self, db) -> None:
        """没有目标行：源域 P（含补偿）与 Q 归一到同一人，Q=80 把 P 的 70 吃掉——补偿同样没落地"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await source_merge("P", "A", person_score=20, alias_score=70)
        await seed_affection(self_domain(), platform_userid="Q", preset_id=0, score=80)

        await join_group(group)
        assert await score_of(group.id, "Q") == 80

        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "Q") == 80

    async def test_neutralised_events_stay_in_the_timeline_with_their_origin(self, db) -> None:
        """改记的是来源类别：条数不丢、增量不改、来源账号仍是 A、跟着挂到最终的人名下"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(target, platform_userid="Q", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)

        events = await DBAffectionEvent.filter(**target.as_columns(), platform_userid="Q").order_by("id")
        carried = [event for event in events if event.source_platform_userid == "A"]
        assert len(carried) == 1
        assert carried[0].delta == 50
        assert carried[0].source == AffectionEventSource.DOMAIN_MERGE.value
        assert not [
            event
            for event in events
            if event.source in (AffectionEventSource.MERGE.value, AffectionEventSource.UNMERGE.value)
        ]

    async def test_row_moved_whole_keeps_its_compensation_revocable(self, db) -> None:
        """反向对照：没有任何折叠、整行搬过去（含归一改键），补偿真的落了地，撤销 A 照旧扣回 50"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)
        assert await score_of(group.id, "Q") == 70

        result = await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert result.total_delta == -50
        assert await score_of(group.id, "Q") == 20

    async def test_unremapped_whole_move_keeps_its_compensation_revocable(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)
        assert await score_of(group.id, "P") == 70

        result = await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert result.total_delta == -50
        assert await score_of(group.id, "P") == 20

    async def test_other_people_in_the_same_preset_are_judged_separately(self, db) -> None:
        """同一个人设下别人的补偿不受牵连：P 折叠了（A→P 的补偿不可撤销），Z 整行搬过去（Y→Z 照旧可撤销）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)
        await source_merge("Z", "Y", person_score=10, alias_score=40)

        await join_group(group)
        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        result = await unmerge_account(target, alias_platform_userid="Y", operator="ops")

        assert await score_of(group.id, "P") == 90
        assert await score_of(group.id, "Z") == 10
        assert result.total_delta == -30

    async def test_other_presets_of_the_same_person_are_judged_separately(self, db) -> None:
        """人设维独立：人设 0 上折叠了、人设 7 上整行搬过去——只有人设 0 的补偿被中和"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=90)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=20)
        await seed_affection(self_domain(), platform_userid="A", preset_id=0, score=70)
        await seed_affection(self_domain(), platform_userid="P", preset_id=7, score=10)
        await seed_affection(self_domain(), platform_userid="A", preset_id=7, score=40)
        await merge_accounts(self_domain(), person_uid="P", alias_platform_userid="A", operator="ops")

        await join_group(group)
        result = await unmerge_account(target, alias_platform_userid="A", operator="ops")

        scores = {
            row.preset_id: row.score
            for row in await DBAffection.filter(**target.as_columns(), platform_userid="P")
        }
        assert scores == {0: 90, 7: 10}
        assert [(transfer.preset_id, transfer.delta) for transfer in result.transfers] == [(7, -30)]


SECOND_INSTANCE = "inst2"


async def join_second_instance(group: DBInstanceGroup) -> None:
    """第二个实例（自成一域期间有数据）再并入同一个组域"""
    await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=SECOND_INSTANCE, group=group, operator="ops")


class TestTargetSideCompensationIsNeutralisedToo:
    """目标域行上已有的账号合并补偿，在该 (人, 人设) 发生折叠后同样不可撤销

    只把**源域**事件改记成 `domain_merge` 是不够的：目标域那一行自己身上的 `merge` 补偿仍可撤销。
    折叠把分数改成「取高」之后，这笔补偿是否还在分数里已无法逐笔对账，撤销会扣掉别处挣的分——
    「宁可少扣，不能多扣」不成立。统一口径：落在发生过折叠的 (目标人, 人设) 上的补偿事件，
    不论来自源域还是目标域，一律改记为 `domain_merge`。
    """

    async def test_inst1_whole_row_with_compensation_then_inst2_folds(self, db) -> None:
        """inst1 的 P=20、A=70 整行入组得 P=70；inst2 的 P=90 再入组折叠：撤销 A 后 P 仍是 90（原来是 40）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await source_merge("P", "A", person_score=20, alias_score=70)
        await join_group(group)
        assert await score_of(group.id, "P") == 70
        await seed_affection(
            ScopeDomain.of_instance(ADAPTER, SECOND_INSTANCE), platform_userid="P", preset_id=0, score=90
        )

        await join_second_instance(group)
        assert await score_of(group.id, "P") == 90

        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 90, "目标行上的 merge 补偿仍可撤销：撤销 A 扣掉了 P 自己的分"

    async def test_target_borrowed_from_alias_then_higher_source_folds(self, db) -> None:
        """目标域 P=30、A=60 已合并（P=60，借 30）；源 P=80 折叠为 80：撤销 A 后仍是 80（原来是 50）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=30)
        await seed_affection(target, platform_userid="A", preset_id=0, score=60)
        await merge_accounts(target, person_uid="P", alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 60
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80)

        await join_group(group)
        assert await score_of(group.id, "P") == 80

        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 80

    async def test_target_borrowed_from_alias_then_lower_source_folds(self, db) -> None:
        """目标域 P=60、A=90 已合并（P=90，借 30）；源 P=70 折叠（分数不变）：撤销 A 后仍是 90（原来是 60）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=60)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await merge_accounts(target, person_uid="P", alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 90
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=70)

        await join_group(group)
        assert await score_of(group.id, "P") == 90

        await unmerge_account(target, alias_platform_userid="A", operator="ops")
        assert await score_of(group.id, "P") == 90

    async def test_target_compensation_of_uncollided_people_stays_revocable(self, db) -> None:
        """反向对照：折叠的是 P；目标域上 Z 的补偿（Z 没有被折叠）照旧可撤销"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=60)
        await seed_affection(target, platform_userid="Z", preset_id=0, score=10)
        await seed_affection(target, platform_userid="Y", preset_id=0, score=40)
        await merge_accounts(target, person_uid="Z", alias_platform_userid="Y", operator="ops")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=70)

        await join_group(group)
        result = await unmerge_account(target, alias_platform_userid="Y", operator="ops")

        assert result.total_delta == -30
        assert await score_of(group.id, "Z") == 10

    async def test_target_compensation_events_stay_in_the_timeline(self, db) -> None:
        """中和的是来源类别：目标行上那条补偿的条数、增量、来源账号都不变"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=30)
        await seed_affection(target, platform_userid="A", preset_id=0, score=60)
        await merge_accounts(target, person_uid="P", alias_platform_userid="A", operator="ops")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80)

        await join_group(group)

        events = await DBAffectionEvent.filter(**target.as_columns(), platform_userid="P", source_platform_userid="A")
        assert [(event.delta, event.source) for event in events] == [(30, AffectionEventSource.DOMAIN_MERGE.value)]


async def unmerge_events(group_id: int, account: str) -> list:
    """目标域里某副号名下仍可撤销的补偿事件的增量"""
    domain = ScopeDomain.of_group(group_id)
    rows = await DBAffectionEvent.filter(
        **domain.as_columns(),
        source_platform_userid=account,
        source__in=[AffectionEventSource.MERGE.value, AffectionEventSource.UNMERGE.value],
    ).order_by("id")
    return [row.delta for row in rows]


class TestMovedAliasNeverLowersTheAccountsOwnTargetScore:
    """随迁别名 `A→P`：目标域里 A 本来有活跃好感时，别名落地后 A 读到的是 P 的行

    目标 A=90、源域 P 补到 70，别名随迁之后 A 读到 70——一次悄悄的降分。
    取舍：把 P 补到不低于 A，补偿记 `merge` 事件、来源账号 A，撤销 A 即扣回，
    与账号合并同一口径；不选「列入冲突」（那样每个在目标域有过发言的副号都要运营事后手工合并）。
    """

    async def test_person_is_lifted_to_the_alias_own_target_score(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)

        assert await resolve_person_uid(target, "A") == "P"
        assert await score_of(group.id, "P") == 90, "A 读到的是 P 的行：90 掉成了 70"

    async def test_compensation_is_revocable_and_gives_back_exactly_what_landed(self, db) -> None:
        """补偿记 merge、来源账号 A：撤销 A 把源域那笔 50 与这笔 20 一起扣回，A 回到自己的 90"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)
        assert await unmerge_events(group.id, "A") == [50, 20]

        result = await unmerge_account(target, alias_platform_userid="A", operator="ops")

        assert result.total_delta == -70
        assert await score_of(group.id, "P") == 20
        assert await score_of(group.id, "A") == 90

    async def test_no_compensation_when_the_person_is_already_higher(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=40)
        await source_merge("P", "A", person_score=20, alias_score=70)

        await join_group(group)

        assert await score_of(group.id, "P") == 70
        assert await unmerge_events(group.id, "A") == [50], "P 已经不低于 A，不该再记一笔补偿"

    async def test_compensation_follows_the_target_rewrite_to_the_final_person(self, db) -> None:
        """目标域已有 P→Q：源别名 A→P 改写为 A→Q，补偿补到 Q 的行上"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(target, platform_userid="Q", preset_id=0, score=30)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await add_alias(self_domain(), "A", "P")

        await join_group(group)

        assert await target_aliases(group.id) == {"P": "Q", "A": "Q"}
        assert await score_of(group.id, "Q") == 90

    async def test_each_preset_is_judged_separately(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await seed_affection(target, platform_userid="A", preset_id=7, score=20)
        await seed_affection(target, platform_userid="P", preset_id=7, score=50)
        await add_alias(self_domain(), "A", "P")

        await join_group(group)

        domain = ScopeDomain.of_group(group.id)
        scores = {
            row.preset_id: row.score for row in await DBAffection.filter(**domain.as_columns(), platform_userid="P")
        }
        assert scores == {0: 90, 7: 50}

    async def test_two_aliases_of_the_same_person_both_count(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A1", preset_id=0, score=90)
        await seed_affection(target, platform_userid="A2", preset_id=0, score=95)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=10)
        await add_alias(self_domain(), "A1", "P")
        await add_alias(self_domain(), "A2", "P")

        await join_group(group)

        assert await score_of(group.id, "P") == 95
        assert sorted(await unmerge_events(group.id, "A1")) == [80]
        assert sorted(await unmerge_events(group.id, "A2")) == [5]

    async def test_dormant_source_row_of_the_alias_is_not_compensated_again(self, db) -> None:
        """源域里 A 休眠的那一行当初已补偿过 P；P 之后合法下跌，并入不把它翻出来再补一遍"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        await source_merge("P", "A", person_score=20, alias_score=70)
        source_key = AffectionKey.of_domain(self_domain(), person_uid="P", preset_id=0)

        await apply_delta(source_key, delta=-40, reason="跌", source=AffectionEventSource.MANUAL, operator="ops")

        await join_group(group)

        assert await score_of(group.id, "P") == 30
        assert await unmerge_events(group.id, "A") == [50]

    async def test_aliases_without_a_target_row_cause_no_compensation(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=10)
        await add_alias(self_domain(), "A", "P")

        await join_group(group)

        assert await score_of(group.id, "P") == 10
        assert await unmerge_events(group.id, "A") == []

    async def test_preview_names_the_aliases_that_will_lift_the_person(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=70)

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())

        text = "\n".join(preview.notes)
        assert "本来就有自己的好感记录" in text and "A" in text

    async def test_preview_is_silent_when_no_alias_has_a_target_row(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await source_merge("P", "A", person_score=20, alias_score=70)

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())

        assert "本来就有自己的好感记录" not in "\n".join(preview.notes)


class TestFoldReadsTargetRowsUnderLock:
    """折叠前对目标行加行锁读

    SQLite 没有行锁（Tortoise 在该方言下忽略它），这里只能证明「调用点在、锁的是对的行、且能跑」；
    真正的互斥见 `test_domain_join_concurrency_pg.py`（真 PG）。
    """

    @pytest.fixture
    def lock_calls(self, monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
        calls: List[tuple] = []
        original = QuerySet.select_for_update

        def spy(self: QuerySet, *args, **kwargs):
            calls.append((self.model.__name__, tuple(self._orderings)))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", spy)
        return calls

    async def test_target_rows_are_read_with_select_for_update(self, db, lock_calls) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=50)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80)

        await join_group(group)

        assert any(model == "DBAffection" for model, _ in lock_calls), "折叠读目标行没有加行锁"
        assert await score_of(group.id, "P") == 80

    async def test_locks_are_taken_in_a_fixed_order(self, db, lock_calls) -> None:
        """加锁顺序固定为 (人, 人设) 升序：两次并发并入对重叠的行同序加锁，不死锁"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80)

        await join_group(group)

        orderings = [ordering for model, ordering in lock_calls if model == "DBAffection"]
        assert orderings and all(len(ordering) == 2 for ordering in orderings)

    async def test_large_source_is_locked_in_batches_and_still_folds_correctly(self, db, monkeypatch) -> None:
        """候选人数超过一批的上限时分批锁行，折叠结果不变"""
        monkeypatch.setattr("nekro_agent.services.identity_admin._LOCK_BATCH", 2)
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        for index, uid in enumerate(["u1", "u2", "u3", "u4", "u5"]):
            await seed_affection(target, platform_userid=uid, preset_id=0, score=10)
            await seed_affection(self_domain(), platform_userid=uid, preset_id=0, score=20 + index)

        await join_group(group)

        assert {uid: await score_of(group.id, uid) for uid in ["u1", "u2", "u3", "u4", "u5"]} == {
            "u1": 20,
            "u2": 21,
            "u3": 22,
            "u4": 23,
            "u5": 24,
        }
        assert await DBAffection.filter(**target.as_columns()).count() == 5

    async def test_untouched_people_are_not_locked(self, db, monkeypatch) -> None:
        """只锁候选人名下的行：组域里与本次并入无关的人不进锁范围（整表锁会让整组的写入排队）"""
        seen: List[List[str]] = []
        original = QuerySet.select_for_update

        def spy(self: QuerySet, *args, **kwargs):
            if self.model is DBAffection:
                for q_object in self._q_objects:
                    seen.append(sorted(q_object.filters.get("platform_userid__in", [])))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", spy)
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="P", preset_id=0, score=50)
        await seed_affection(target, platform_userid="bystander", preset_id=0, score=50)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80)

        await join_group(group)

        locked = {uid for batch in seen for uid in batch}
        assert "P" in locked
        assert "bystander" not in locked


class TestMovedAliasesNeverFormChains:
    """搬进目标域的别名不能与目标域已有别名接成链（解析只跟一跳）

    取舍：源别名 A→P 撞上目标域 P→Q 时**改写为 A→Q**
    （跟到终点，备注留痕、响应里逐个点名）；其余会成链的形态——目标域已有别名指向 A、
    或改写后指回 A 自己——不改目标域既有的任何一行，把源别名列入冲突、留在原地。
    """

    async def test_source_alias_onto_a_target_secondary_is_rewritten_to_the_final_person(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await add_alias(self_domain(), "A", "P")

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert await target_aliases(group.id) == {"P": "Q", "A": "Q"}
        assert await chain_violations(target.domain_id) == []
        assert await resolve_person_uid(target, "A") == "Q"
        moved = await DBIdentityAlias.filter(domain_id=target.domain_id, platform_userid="A").first()
        assert moved is not None and "P" in moved.note and "Q" in moved.note, "改写没有在别名备注里留痕"
        assert response.migration is not None
        assert response.migration.alias_rows == 1
        assert response.migration.alias_conflicts == []
        assert len(response.migration.alias_rewrites) == 1
        assert "A" in response.migration.alias_rewrites[0]

    async def test_target_alias_pointing_at_the_moving_account_is_a_conflict_and_target_is_untouched(self, db) -> None:
        """目标域 X→A，源域 A→P：搬过去就是 X→A→P。目标域的既有行不改，源别名留下并点名"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "X", "A")
        await add_alias(self_domain(), "A", "P")

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert await target_aliases(group.id) == {"X": "A"}
        assert await chain_violations(target.domain_id) == []
        left_behind = await DBIdentityAlias.filter(domain_id=self_domain().domain_id, platform_userid="A").first()
        assert left_behind is not None and left_behind.person_uid == "P"
        assert response.migration is not None
        assert response.migration.alias_rows == 0
        assert response.migration.alias_conflicts == ["A"]

    async def test_rewrite_that_points_back_at_the_account_itself_is_a_conflict(self, db) -> None:
        """目标域 P→A，源域 A→P：跟到终点就是 A→A，一条自指别名——必须拒"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "A")
        await add_alias(self_domain(), "A", "P")

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert await target_aliases(group.id) == {"P": "A"}
        assert response.migration is not None and response.migration.alias_conflicts == ["A"]

    async def test_self_loop_reason_is_told_to_the_operator(self, db) -> None:
        """预览里点名的原因要如实：这里是「指回自己」，不是别的冲突"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        await add_alias(ScopeDomain.of_group(group.id), "P", "A")
        await add_alias(self_domain(), "A", "P")

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())

        assert any("A" in note and "指回它自己" in note for note in preview.notes)

    async def test_degenerate_self_pointing_source_alias_is_never_moved(self, db) -> None:
        """源域里的脏行 A→A（指向自己）：搬过去就是一条自指别名，按冲突留下"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(self_domain(), "A", "A")

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert await target_aliases(group.id) == {}
        assert await chain_violations(target.domain_id) == []
        assert response.migration is not None and response.migration.alias_conflicts == ["A"]

    async def test_whatever_the_mix_the_target_domain_ends_chain_free(self, db) -> None:
        """混合场景：同时有可改写的、会成链的、同账号冲突的、无关的别名"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        for account, person in (("P", "Q"), ("X", "A2"), ("U9", "T-main")):
            await add_alias(target, account, person)
        for account, person in (("A", "P"), ("A2", "P"), ("U9", "other"), ("B", "Z"), ("C", "Q")):
            await add_alias(self_domain(), account, person)

        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        aliases = await target_aliases(group.id)
        assert await chain_violations(target.domain_id) == []
        # 目标域既有的三行一个字都没变
        assert aliases["P"] == "Q" and aliases["X"] == "A2" and aliases["U9"] == "T-main"
        # 可改写的跟到终点、无关的原样
        assert aliases["A"] == "Q" and aliases["B"] == "Z" and aliases["C"] == "Q"
        # A2 被目标域的 X 指着，搬过去就是 X→A2→P：不迁，目标域那行也不改
        assert "A2" not in aliases


class TestPreexistingChainInTargetIsNotExtended:
    async def test_alias_whose_final_person_is_itself_a_secondary_is_left_behind(self, db) -> None:
        """目标域自己已经有链（P→Q、Q→R，不变式被脏数据破过）：跟一跳落不到终点，再迁只会让链更长"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await add_alias(target, "Q", "R")
        await add_alias(self_domain(), "A", "P")

        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert await target_aliases(group.id) == {"P": "Q", "Q": "R"}
        assert response.migration is not None and response.migration.alias_conflicts == ["A"]


class TestSnapshotCacheDoesNotHideMigratedRows:
    """目标域的键上可能早就缓存着「从未打过交道」的空快照（组里别的成员实例一直在读这个域）"""

    async def _warm_empty_snapshot(self, domain: ScopeDomain, uid: str, preset_id: int) -> AffectionKey:
        key = AffectionKey.of_domain(domain, person_uid=uid, preset_id=preset_id)
        invalidate_cache()
        assert (await load_snapshot(key, ttl_seconds=600)).exists is False
        return key

    async def test_plain_move_is_visible_immediately(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=42)
        key = await self._warm_empty_snapshot(target, "u1", 0)

        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        snapshot = await load_snapshot(key, ttl_seconds=600)
        assert snapshot.exists and snapshot.score == 42, "并入的行被目标键上缓存的空快照盖住了"

    async def test_rekeyed_move_is_visible_under_the_final_person(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=55)
        key = await self._warm_empty_snapshot(target, "Q", 0)

        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        snapshot = await load_snapshot(key, ttl_seconds=600)
        assert snapshot.exists and snapshot.score == 55


class TestDomainJoinIsStillOneTransaction:
    """归一化把「先搬行后搬别名」改成了「先定别名再搬行」，原子性不能因此丢"""

    async def test_failure_midway_rolls_everything_back(self, db, monkeypatch) -> None:
        from nekro_agent.services import identity_admin

        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await add_alias(self_domain(), "A", "P")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=70, events=1)
        await seed_affection(target, platform_userid="Q", preset_id=0, score=30)

        async def boom(*args, **kwargs):
            raise RuntimeError("折叠补偿事件写失败")

        monkeypatch.setattr(identity_admin, "apply_delta", boom)

        with pytest.raises(RuntimeError):
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
                _current_user=acme_admin(),
            )

        assert await DBInstanceDomainBinding.filter(instance_key=OWN_INSTANCE).count() == 0
        assert [(row.platform_userid, row.score) for row in await self_domain_rows()] == [("P", 70)]
        assert [(row.platform_userid, row.score) for row in await group_domain_rows(group.id)] == [("Q", 30)]
        assert await target_aliases(group.id) == {"P": "Q"}
        source_alias = await DBIdentityAlias.filter(domain_id=self_domain().domain_id).first()
        assert source_alias is not None and source_alias.person_uid == "P"


    async def test_standalone_call_is_atomic_on_its_own(self, db, monkeypatch) -> None:
        """不经 `set_instance_domain`（没有外层事务）直接调用，函数自己也得整体回滚

        故障注入点放在**最后一步**（搬人档案）：此时行、流水、别名都已经写完，
        回滚得到「原样」才说明前面那些写入真的都在同一个事务里。
        """
        from nekro_agent.services import identity_admin

        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await add_alias(self_domain(), "A", "P")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=70, events=1)
        await seed_affection(target, platform_userid="Q", preset_id=0, score=30)
        await DBIdentityPerson.create(domain_id=self_domain().domain_id, person_uid="P", display_name="甲")

        async def boom(self, *args, **kwargs):
            raise RuntimeError("搬人档案写失败")

        monkeypatch.setattr(DBIdentityPerson, "save", boom)

        with pytest.raises(RuntimeError):
            await identity_admin.merge_self_domain_into_group(
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                group=group,
            )

        assert [(row.platform_userid, row.score) for row in await self_domain_rows()] == [("P", 70)]
        assert [(row.platform_userid, row.score) for row in await group_domain_rows(group.id)] == [("Q", 30)]
        assert await target_aliases(group.id) == {"P": "Q"}
        source_alias = await DBIdentityAlias.filter(domain_id=self_domain().domain_id).first()
        assert source_alias is not None and source_alias.person_uid == "P"
        assert await DBAffectionEvent.filter(**self_domain().as_columns()).count() == 1
        assert await DBAffectionEvent.filter(**target.as_columns()).count() == 0


class TestPreviewReflectsNormalisation:
    """预览必须如实反映归一化与别名改写——同一场景下与执行结果对拍"""

    async def _seed_mixed(self, group: DBInstanceGroup) -> None:
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await add_alias(target, "X", "A2")
        await add_alias(self_domain(), "A", "P")
        await add_alias(self_domain(), "A2", "P")
        await add_alias(self_domain(), "B", "Z")
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=70, events=2)
        await seed_affection(self_domain(), platform_userid="Q", preset_id=0, score=20)
        await seed_affection(self_domain(), platform_userid="Z", preset_id=0, score=5)
        await seed_affection(target, platform_userid="Q", preset_id=1, score=9)
        await seed_affection(target, platform_userid="Z", preset_id=0, score=1)
        await seed_affection(self_domain(), platform_userid="P", preset_id=1, score=33)

    async def test_preview_numbers_equal_what_execution_does(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await self._seed_mixed(group)

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())
        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        migration = response.migration
        assert migration is not None
        assert preview.affection_rows == migration.affection_rows + migration.folded_rows
        assert preview.fold_pairs == migration.folded_rows
        assert preview.remapped_rows == migration.remapped_rows
        assert preview.alias_rows == migration.alias_rows
        assert preview.alias_conflicts == migration.alias_conflicts
        assert preview.alias_rewrites == migration.alias_rewrites
        assert preview.event_rows == migration.event_rows
        # 这个场景确实触发了每一类（否则对拍是空转）
        assert migration.folded_rows >= 2 and migration.remapped_rows >= 2
        assert migration.alias_conflicts == ["A2"] and len(migration.alias_rewrites) == 1

    async def test_preview_states_rewrites_and_conflicts_in_the_notes(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await self._seed_mixed(group)

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())

        text = "\n".join(preview.notes)
        assert "A2" in text, "会成链而被留下的别名没有在预览里点名"
        assert "改写" in text and "A" in text, "别名改写没有在预览里说明"
        assert preview.alias_rewrites and "A" in preview.alias_rewrites[0]

    async def test_preview_does_not_write_anything(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await self._seed_mixed(group)
        before = (
            await DBAffection.all().count(),
            await DBAffectionEvent.all().count(),
            sorted((r.domain_id, r.platform_userid, r.person_uid) for r in await DBIdentityAlias.all()),
        )

        await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())

        after = (
            await DBAffection.all().count(),
            await DBAffectionEvent.all().count(),
            sorted((r.domain_id, r.platform_userid, r.person_uid) for r in await DBIdentityAlias.all()),
        )
        assert before == after


class TestPreviewMatchesExecution:
    """预览先算给操作者看的那份，必须与执行同一口径"""

    async def test_preview_counts_and_conflicts(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(self_domain(), platform_userid="u1", preset_id=0, score=10, events=2)
        await seed_affection(self_domain(), platform_userid="u2", preset_id=0, score=20)
        await seed_affection(target, platform_userid="u1", preset_id=0, score=5)  # 与 u1/preset 0 撞
        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid="u1-old",
            person_uid="u1",
            source=ALIAS_SOURCE_MANUAL,
        )

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())

        assert preview.can_migrate is True
        assert preview.blockers == []
        assert preview.source_domain_id == self_domain().domain_id
        assert preview.target_domain_id == target.domain_id
        assert preview.affection_rows == 2
        assert preview.affection_people == 2
        assert preview.event_rows == 2
        assert preview.fold_pairs == 1
        assert preview.alias_rows == 1

    async def test_preview_blocks_when_instance_is_already_in_a_group(self, db) -> None:
        first = await make_group("g-first", ACME_TENANT_ID)
        second = await make_group("g-second", ACME_TENANT_ID)
        await DBInstanceDomainBinding.bind_to_groups(ADAPTER, OWN_INSTANCE, memory_group=first)

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=second.id, _current_user=acme_admin())

        assert preview.can_migrate is False
        assert preview.blockers != []

    async def test_preview_is_tenant_scoped(self, db) -> None:
        """预览端点与执行端点同一组守卫：别人的实例与别人的组都 404"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        rival_group = await make_group("rival-family", RIVAL_TENANT_ID)

        with pytest.raises(NotFoundError):
            await get_domain_join_preview(FOREIGN_INSTANCE, group_id=group.id, _current_user=acme_admin())
        with pytest.raises(NotFoundError):
            await get_domain_join_preview(OWN_INSTANCE, group_id=rival_group.id, _current_user=acme_admin())


class TestEventsKeepTheirOriginWhenRekeyed:
    """流水改挂到最终的人名下时，空的来源账号（= 与 platform_userid 相同）不能丢

    生产里没合并过的人，事件的 `source_platform_userid` 是空串。P 的事件改挂到 Q 之后，
    空串就会读成「是 Q 自己干的」——原来是 P 的信息丢了。改挂时把空串落成原 uid。
    与 `TestSourceMergeCompensationSurvivesTheJoinOnlyIfItLanded` 的撤销语义不冲突：撤销求和只认补偿来源，而空串事件都不是补偿事件。
    """

    async def _seed_plain_events(self, uid: str, score: int) -> None:
        await seed_affection(self_domain(), platform_userid=uid, preset_id=0, score=score)
        await DBAffectionEvent.filter(**self_domain().as_columns(), platform_userid=uid).delete()
        for index in range(2):
            await DBAffectionEvent.create(
                **self_domain().as_columns(),
                platform_userid=uid,
                preset_id=0,
                source_platform_userid="",
                delta=1,
                score_after=score,
                source="llm",
                reason=f"plain-{uid}-{index}",
            )

    async def test_rekeyed_events_record_the_original_account(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await self._seed_plain_events("P", 40)
        await self._seed_plain_events("Z", 15)

        await join_group(group)

        on_q = await DBAffectionEvent.filter(**target.as_columns(), platform_userid="Q")
        assert len(on_q) == 2 and {event.source_platform_userid for event in on_q} == {"P"}
        # 没被归一的账号原样：空串仍是空串
        on_z = await DBAffectionEvent.filter(**target.as_columns(), platform_userid="Z")
        assert len(on_z) == 2 and {event.source_platform_userid for event in on_z} == {""}

    async def test_origin_account_does_not_make_the_events_revocable(self, db) -> None:
        """撤销 P→Q 这条别名：来源是 P 的普通事件不是补偿，不参与扣回"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await add_alias(target, "P", "Q")
        await self._seed_plain_events("P", 40)

        await join_group(group)
        before = await score_of(group.id, "Q")
        result = await unmerge_account(target, alias_platform_userid="P", operator="ops")

        assert result.total_delta == 0
        assert await score_of(group.id, "Q") == before == 40


class TestCacheIsInvalidatedOnlyAfterCommit:
    """并入的快照缓存失效必须在外层事务提交之后

    提交之前失效，并发读者在失效与提交之间读到旧值又缓存起来，并入的好感在 TTL 内读不到。
    用记录器包住两处 `invalidate_cache`，在调用瞬间看当前连接是不是事务连接。
    """

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> List[bool]:
        from tortoise import connections
        from tortoise.backends.base.client import TransactionalDBClient

        from nekro_agent.routers.console import identity as identity_router
        from nekro_agent.services import identity_admin

        seen: List[bool] = []

        def record(*args, **kwargs) -> None:
            seen.append(isinstance(connections.get("default"), TransactionalDBClient))
            invalidate_cache(*args, **kwargs)

        monkeypatch.setattr(identity_admin, "invalidate_cache", record)
        monkeypatch.setattr(identity_router, "invalidate_cache", record)
        return seen

    async def test_router_invalidates_after_the_transaction_commits(self, db, calls: List[bool]) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=30)

        await join_group(group)

        assert calls, "并入搬了行却没有失效快照缓存"
        assert not any(calls), "缓存在外层事务提交之前失效了"

    async def test_standalone_call_invalidates_after_its_own_commit(self, db, calls: List[bool]) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=30)

        await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=OWN_INSTANCE, group=group)

        assert calls and not any(calls)

    async def test_nothing_to_move_means_no_invalidation(self, db, calls: List[bool]) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)

        await join_group(group)

        assert calls == []


class TestPreviewPersonCountMatchesExecution:
    """预览的「人档案随迁」数必须用与执行相同的撞车判据"""

    async def test_colliding_person_is_counted_the_same_way_in_both(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await DBIdentityPerson.create(domain_id=target.domain_id, person_uid="p1", display_name="目标域的甲")
        await DBIdentityPerson.create(domain_id=self_domain().domain_id, person_uid="p1", display_name="源域的甲")
        await DBIdentityPerson.create(domain_id=self_domain().domain_id, person_uid="p2", display_name="乙")
        await DBIdentityPerson.create(domain_id=self_domain().domain_id, person_uid="p3", display_name="丙")

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())
        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        migration = response.migration
        assert migration is not None
        assert migration.person_conflicts == ["p1"] and migration.person_rows == 2
        assert preview.person_rows == migration.person_rows
        text = "\n".join(preview.notes)
        assert "p1" in text and "撞车" in text, "预览没有点名撞车而不随迁的人档案"
        # 目标域那份没被覆盖
        kept = await DBIdentityPerson.get(domain_id=target.domain_id, person_uid="p1")
        assert kept.display_name == "目标域的甲"

    async def test_all_colliding_means_zero_moves_in_both(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await DBIdentityPerson.create(domain_id=target.domain_id, person_uid="p1", display_name="甲")
        await DBIdentityPerson.create(domain_id=self_domain().domain_id, person_uid="p1", display_name="甲'")

        preview = await get_domain_join_preview(OWN_INSTANCE, group_id=group.id, _current_user=acme_admin())
        response = await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
            _current_user=acme_admin(),
        )

        assert response.migration is not None
        assert preview.person_rows == response.migration.person_rows == 0


class TestDormantScoreFoldedIntoTheHeldRowIsNotCompensatedAgain:
    """补偿只看别名落地**之前**目标域里副号的行分

    目标 A=50，源域 A=95（休眠）、P 当初被补到 95 后合法跌到 70。并入时源域那行 A 折进目标域的 A（50→95），
    若补偿读折叠后的副号分 95，会把 P 又补回 95；该有的是 70（目标域原来的 A=50 低于 70，无需补）。
    """

    async def test_p_keeps_its_legitimate_drop(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=50)
        await source_merge("P", "A", person_score=20, alias_score=95)
        source_key = AffectionKey.of_domain(self_domain(), person_uid="P", preset_id=0)
        await apply_delta(source_key, delta=-25, reason="跌", source=AffectionEventSource.MANUAL, operator="ops")

        await join_group(group)

        assert await score_of(group.id, "P") == 70, "休眠的 95 分被补偿翻出来又补了一遍"
        assert await unmerge_events(group.id, "A") == [75], "只该留源域那一笔补偿，目标域不该再补"

    async def test_control_a_held_row_that_really_is_higher_still_compensates(self, db) -> None:
        """对照：目标域原来的 A=90 高于 P=70，补偿照常发生，补到 90（不是折叠后的 95）"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await source_merge("P", "A", person_score=20, alias_score=95)
        source_key = AffectionKey.of_domain(self_domain(), person_uid="P", preset_id=0)
        await apply_delta(source_key, delta=-25, reason="跌", source=AffectionEventSource.MANUAL, operator="ops")

        await join_group(group)

        assert await score_of(group.id, "P") == 90
        assert await unmerge_events(group.id, "A") == [75, 20]


class TestCompensationOnlyJoinStillInvalidatesTheCache:
    """只有补偿、没有搬行也没有折叠的并入，同样要失效快照缓存"""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> List[bool]:
        from nekro_agent.routers.console import identity as identity_router

        seen: List[bool] = []

        def record(*args, **kwargs) -> None:
            seen.append(True)
            invalidate_cache(*args, **kwargs)

        monkeypatch.setattr(identity_router, "invalidate_cache", record)
        return seen

    async def _setup_only_compensation(self, group: DBInstanceGroup) -> None:
        target = ScopeDomain.of_group(group.id)
        await seed_affection(target, platform_userid="A", preset_id=0, score=90)
        await seed_affection(target, platform_userid="P", preset_id=0, score=20)
        await add_alias(self_domain(), "A", "P")  # 源域只有这一条别名，没有任何好感行

    async def test_migration_reports_the_compensation(self, db) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await self._setup_only_compensation(group)

        migration = await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=OWN_INSTANCE, group=group)

        assert (migration.affection_rows, migration.folded_rows) == (0, 0)
        assert migration.alias_compensated == 70

    async def test_router_invalidates_after_commit(self, db, calls: List[bool]) -> None:
        group = await make_group("acme-family", ACME_TENANT_ID)
        await self._setup_only_compensation(group)
        # 把目标键上的旧值先缓存进去：不失效的话读到的还是 20
        assert await score_of(group.id, "P") == 20

        await join_group(group)

        assert calls, "补偿改了 P 的分数，路由却没有失效缓存"
        assert await score_of(group.id, "P") == 90


class TestUniqueCollisionBecomesACleanRetryableError:
    """并入撞上唯一约束（运行时写入刚好在锁读与搬行之间建了目标行）= 清晰的 409，不是 500（A5）"""

    def test_deadlock_and_serialisation_failures_count_as_write_conflicts(self) -> None:
        """asyncpg 的异常类不在模块顶层导入，按类名认；任意别的异常不算"""
        from nekro_agent.services.identity_admin import _is_write_conflict

        class DeadlockDetectedError(Exception):
            pass

        class SerializationError(Exception):
            pass

        assert _is_write_conflict(DeadlockDetectedError())
        assert _is_write_conflict(SerializationError())
        assert not _is_write_conflict(RuntimeError("boom"))
        assert not _is_write_conflict(ValueError("模型层校验"))

    async def test_collision_is_translated_and_everything_rolls_back(self, db, monkeypatch) -> None:
        from nekro_agent.schemas.errors import ConcurrentWriteConflictError
        from nekro_agent.services import identity_admin

        group = await make_group("acme-family", ACME_TENANT_ID)
        target = ScopeDomain.of_group(group.id)
        await seed_affection(self_domain(), platform_userid="P", preset_id=0, score=80)
        real = identity_admin._lock_target_rows

        async def sneaky(*args, **kwargs):
            rows = await real(*args, **kwargs)
            # 锁读时目标域里还没有 P；之后另一个写入者把它建出来了
            await seed_affection(target, platform_userid="P", preset_id=0, score=10)
            return rows

        monkeypatch.setattr(identity_admin, "_lock_target_rows", sneaky)

        with pytest.raises(ConcurrentWriteConflictError) as excinfo:
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=group.id, migrate_affection=True),
                _current_user=acme_admin(),
            )

        assert excinfo.value.http_status == 409
        assert "重试" in str(excinfo.value)
        # 整笔回滚：绑定没落、源行还在源域、目标域里没有半成品
        assert not (await resolve_memory_domain(ADAPTER, OWN_INSTANCE)).is_group
        assert [row.score for row in await self_domain_rows()] == [80]
        assert [row.score for row in await group_domain_rows(group.id)] == []