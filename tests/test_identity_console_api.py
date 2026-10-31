"""后台「身份与域」的行为契约：跨租户串域、账号合并、换人设确认、人设孤儿数据

`tests/test_route_permission_contract.py` 守的是**结构**（每条端点接了守卫、等级够 Admin）。
本文件守的是**行为**：同样的请求，换一个身份进来会拿到什么。

## 本轮最严重的那条风险面：跨租户串域

域的配置入口如果不设防，租户管理员就能把**别的租户的实例**拉进自己的域，
于是当场读到对方的记忆与好感。这条路径有三扇门，本文件逐扇验：

| 门 | 位置 | 本文件的用例 |
|---|---|---|
| 实例归属 | `tenant_perm.assert_instance_accessible` | `TestDomainConfigIsTenantScoped::test_tenant_cannot_pull_a_foreign_instance_into_its_domain` |
| 组归属 | `_assert_domain_accessible` | `...::test_tenant_cannot_push_its_instance_into_a_foreign_domain` |
| 模型层 | `DBInstanceDomainBinding.bind_to_groups` | `...::test_model_layer_rejects_a_mismatch_the_router_let_through` |

第三扇是"从别的入口进来"的兜底，理由与 #51（模型组写侧归属校验）逐字相同。
每一条越权都断言 **404 而不是 403**：403 会把别人的实例/组变成可枚举的。

## 预览与执行必须给出同一个答案

账号合并的执行在 `services/affection/merge.py`（别名 + 逐人设「取高的」补偿事件），
预览在 `services/identity_admin.py`。两处各有一份"取高的"判据 —— 这正是本项目反复栽跟头的
那个形态（§2.7 / §2.9 ②）。因此这里有一组用例**拿预览与真实合并结果逐字对拍**
（`TestMergePreviewMatchesTheRealMerge`），两边一旦分叉立刻红。

## 换人设的二次确认是"不做就必然变成工单"的那一条

运营在群里把人设从甲换成乙的那一刻，该群所有人的好感当场看起来归零（实为切到另一份）。
两个后台入口（频道、实例默认）都必须先说清楚影响面才允许执行，而"生效人设其实没变"
的情况不能拦 —— 把实例默认人设显式绑到频道上是纯运维动作，拦它只会让人学会一路点确认。
"""

import datetime
from typing import List, Optional, Tuple

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_channel import INSTANCE_METADATA_DEFAULT_PRESET_ID, DBChatChannel
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.affection import get_affection_detail
from nekro_agent.routers.chat_channel import get_chat_channel_preset_impact, set_chat_channel_preset
from nekro_agent.routers.console.identity import (
    DomainCreateRequest,
    DomainUpdateRequest,
    InstanceDomainUpdateRequest,
    MergeRequest,
    OrphanCleanupRequest,
    cleanup_identity_orphans,
    create_identity_domain,
    delete_identity_domain,
    execute_identity_merge,
    get_identity_aliases,
    get_identity_domains,
    get_identity_orphans,
    preview_identity_merge,
    revoke_identity_merge,
    set_instance_domain,
    update_identity_domain,
)
from nekro_agent.routers.presets import (
    InstanceDefaultPresetUpdate,
    delete_preset,
    get_instance_default_preset_impact,
    set_instance_default_preset,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.merge import merge_accounts
from nekro_agent.services.identity_scope import ScopeDomain, resolve_memory_domain
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
OWN_SECOND_INSTANCE = "inst2"
FOREIGN_INSTANCE = "rival1"

# 与测试环境同形：acme 名下 inst1/inst2，另一租户名下 rival1
INSTANCE_LAYOUT: List[Tuple[str, Optional[int]]] = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (OWN_SECOND_INSTANCE, ACME_TENANT_ID),
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
) -> DBAffection:
    """在指定的 (域, 人, 人设) 上建一行好感，并按需补几条流水"""
    record = await DBAffection.create(
        **domain.as_columns(),
        platform_userid=platform_userid,
        preset_id=preset_id,
        nickname=f"nick-{platform_userid}",
        score=score,
        stage="stranger",
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



@pytest.fixture
def captured_warnings():
    """把本次用例里打出来的 WARNING 收进一个列表

    两个坑都踩过了，记在这里：

    1. `get_sub_logger` 回的是 **loguru** 的 logger，不走 stdlib 的 `logging`，
       `caplog` 一条都收不到 —— 用它写的第一版是恒绿的；
    2. `get_sub_logger` 每次调用回的是 `logger.bind(...)` 产生的**新对象**，
       所以 monkeypatch 掉 `nekro_agent.core.logger.logger.warning` 拦不住在函数体内
       现取 logger 的调用方（`AgentCtx.set_preset` 就是这样）—— 第二版也是恒绿的。

    因此这里挂一个真正的 loguru sink：不替换任何被测代码，只在日志的出口接一根线，
    用例收尾时摘掉。
    """
    from nekro_agent.core.logger import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


class TestDomainConfigIsTenantScoped:
    """跨租户串域：三扇门逐扇验，越权一律 404"""

    async def test_tenant_only_sees_its_own_domains_and_instances(self, db) -> None:
        await make_group("acme-family", ACME_TENANT_ID)
        await make_group("rival-family", RIVAL_TENANT_ID)
        await make_group("platform-family", None)

        response = await get_identity_domains(_current_user=acme_admin())

        assert [item.name for item in response.items] == ["acme-family"]
        assert sorted(item.instance_key for item in response.instances) == [OWN_INSTANCE, OWN_SECOND_INSTANCE]
        # 租户没有"给别的租户建组"这一档，前端据此不画那个选择器（不摆点不动的按钮）
        assert response.can_choose_tenant is False

    async def test_platform_owner_sees_everything(self, db) -> None:
        await make_group("acme-family", ACME_TENANT_ID)
        await make_group("platform-family", None)

        response = await get_identity_domains(_current_user=platform_owner())

        assert sorted(item.name for item in response.items) == ["acme-family", "platform-family"]
        assert len(response.instances) == len(INSTANCE_LAYOUT)
        assert response.can_choose_tenant is True

    async def test_tenant_cannot_pull_a_foreign_instance_into_its_domain(self, db) -> None:
        """第一扇门：别人的实例够不着 —— 这是"读到对方记忆与好感"的入口"""
        group = await make_group("acme-family", ACME_TENANT_ID)

        with pytest.raises(NotFoundError):
            await set_instance_domain(
                FOREIGN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=group.id),
                _current_user=acme_admin(),
            )

        assert await DBInstanceDomainBinding.filter(instance_key=FOREIGN_INSTANCE).count() == 0

    async def test_an_unknown_instance_is_404_not_a_crash(self, db) -> None:
        """第一扇门单独成立的判据

        「租户拿别人的实例」那条用例其实被**三扇门**同时挡着（接口层两道 + 模型层一道），
        所以摘掉接口层的实例守卫它照样绿 —— 那是纵深防御的好消息，却让那条用例
        测不出这一扇门还在不在。这一条补上：守卫在，不存在的实例是 404；
        守卫不在，端点会在"实例是 None"上崩成 500。
        """
        group = await make_group("acme-family", ACME_TENANT_ID)

        with pytest.raises(NotFoundError):
            await set_instance_domain(
                "no_such_instance",
                InstanceDomainUpdateRequest(group_id=group.id),
                _current_user=acme_admin(),
            )

    async def test_tenant_cannot_push_its_instance_into_a_foreign_domain(self, db) -> None:
        """第二扇门：别人的组够不着（不存在与无权访问对外不可区分）"""
        foreign_group = await make_group("rival-family", RIVAL_TENANT_ID)

        with pytest.raises(NotFoundError):
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=foreign_group.id),
                _current_user=acme_admin(),
            )

        assert await DBInstanceDomainBinding.filter(instance_key=OWN_INSTANCE).count() == 0

    async def test_tenant_cannot_touch_a_foreign_domain_at_all(self, db) -> None:
        """改名与删除同样 404，否则组 ID 可枚举"""
        foreign_group = await make_group("rival-family", RIVAL_TENANT_ID)
        user = acme_admin()

        with pytest.raises(NotFoundError):
            await update_identity_domain(foreign_group.id, DomainUpdateRequest(name="hijacked"), _current_user=user)
        with pytest.raises(NotFoundError):
            await delete_identity_domain(foreign_group.id, _current_user=user)

        assert (await DBInstanceGroup.get(id=foreign_group.id)).name == "rival-family"

    async def test_model_layer_rejects_a_mismatch_the_router_let_through(self, db) -> None:
        """第三扇门：模型层的兜底

        平台方两道接口门都过得去（组是平台方的、实例是租户的），但
        `assert_instance_joinable` 要求两者的归属**逐字相等**，因此这一步仍然被拒。
        没有这扇门，平台方组就成了把任意租户实例并进同一个域的万能入口。

        **对平台方回 400 并说明原因，不回 404。** 他看得见全部实例与全部组，
        这里没有任何存在性要掩盖；回"实例不存在"是在对着一个他正盯着的行说谎，
        而真实原因（把租户的实例放进平台方的组）他一眼就能改。
        租户一侧的存在性掩盖由上面四条 404 用例守着，两者不冲突。
        """
        platform_group = await make_group("platform-family", None)

        with pytest.raises(ValidationError) as excinfo:
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=platform_group.id),
                _current_user=platform_owner(),
            )
        assert "归属" in str(excinfo.value)

        assert await DBInstanceDomainBinding.filter(instance_key=OWN_INSTANCE).count() == 0

    async def test_a_nonexistent_group_is_404_and_does_not_silently_unbind(self, db) -> None:
        """把组归属那扇门**单独**钉住

        另外两条越权用例（拉别人的实例 / 推进别人的组）其实是被三扇门同时挡着的：
        把接口层的 `_assert_domain_accessible` 换成一句 `get_or_none`，
        模型层的 `assert_instance_joinable` 照样会拒，用例仍然全绿 ——
        纵深防御把这扇门的覆盖率藏起来了（#45 就是这么漏的）。

        这一条挑的是**只有这扇门挡得住**的输入：一个不存在的组 ID。
        少了它，`group` 变成 `None`，`bind_to_groups(memory_group=None)` 的语义是
        「回到自成一域」——于是一个打错的组 ID 会静默地把实例**踢出**它现在的域，
        还回 200。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)

        with pytest.raises(NotFoundError):
            await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=987654), _current_user=user)

        # 原来的域一动没动，而不是被"回到自成一域"顺手抹掉
        assert await resolve_memory_domain(ADAPTER, OWN_INSTANCE) == ScopeDomain.of_group(created.group_id)

    async def test_tenant_can_group_its_own_instances(self, db) -> None:
        """正向对照：同归属下配得通，而且真的改变了域解析

        没有这一条，上面四条"全都 404"可以由一个恒抛 404 的实现满足。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)

        for instance_key in (OWN_INSTANCE, OWN_SECOND_INSTANCE):
            await set_instance_domain(
                instance_key,
                InstanceDomainUpdateRequest(group_id=created.group_id),
                _current_user=user,
            )

        assert await resolve_memory_domain(ADAPTER, OWN_INSTANCE) == ScopeDomain.of_group(created.group_id)
        assert await resolve_memory_domain(ADAPTER, OWN_SECOND_INSTANCE) == ScopeDomain.of_group(created.group_id)
        # 别的租户的实例一点没被牵连
        assert await resolve_memory_domain(ADAPTER, FOREIGN_INSTANCE) == ScopeDomain.of_instance(
            ADAPTER,
            FOREIGN_INSTANCE,
        )

    async def test_tenant_created_domain_belongs_to_its_own_tenant(self, db) -> None:
        """租户建组时请求体里的 `tenant_id` 被忽略 —— 否则建组这一步就能造出串域的容器"""
        created = await create_identity_domain(
            DomainCreateRequest(name="sneaky", tenant_id=RIVAL_TENANT_ID),
            _current_user=acme_admin(),
        )
        assert (await DBInstanceGroup.get(id=created.group_id)).tenant_id == ACME_TENANT_ID

    async def test_platform_owner_can_create_a_domain_for_a_tenant(self, db) -> None:
        """正向对照：平台方要能给客户建组，否则客户的实例根本无处可去"""
        created = await create_identity_domain(
            DomainCreateRequest(name="for-acme", tenant_id=ACME_TENANT_ID),
            _current_user=platform_owner(),
        )
        assert created.tenant_id == ACME_TENANT_ID
        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=created.group_id),
            _current_user=platform_owner(),
        )
        assert await resolve_memory_domain(ADAPTER, OWN_INSTANCE) == ScopeDomain.of_group(created.group_id)

    async def test_leaving_a_domain_deletes_the_binding_row(self, db) -> None:
        """回到自成一域 = **没有行**，而不是一行全 NULL

        零配置的标准形态是"没有行"；留一行全 NULL 会让后台多出一条看不懂的记录，
        也会让"零配置与今天逐字相同"这条回归保险失去一个可断言的判据。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)
        assert await DBInstanceDomainBinding.filter(instance_key=OWN_INSTANCE).count() == 1

        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=None), _current_user=user)

        assert await DBInstanceDomainBinding.filter(instance_key=OWN_INSTANCE).count() == 0
        assert await resolve_memory_domain(ADAPTER, OWN_INSTANCE) == ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)

    async def test_deleting_a_domain_releases_its_members(self, db) -> None:
        """删组必须先释放成员

        `resolve_memory_domain` 刻意不查 `instance_group`：绑定行指向一个已删除的组时
        仍然按那个组 ID 成立。所以留着绑定行的后果不是"回到默认"，而是"一个后台再也
        看不见、成员之间却仍然互通的域"。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        for instance_key in (OWN_INSTANCE, OWN_SECOND_INSTANCE):
            await set_instance_domain(
                instance_key,
                InstanceDomainUpdateRequest(group_id=created.group_id),
                _current_user=user,
            )

        result = await delete_identity_domain(created.group_id, _current_user=user)

        assert result.ok is True
        assert await DBInstanceDomainBinding.filter(memory_group_id=created.group_id).count() == 0
        assert await resolve_memory_domain(ADAPTER, OWN_INSTANCE) == ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        assert await resolve_memory_domain(ADAPTER, OWN_SECOND_INSTANCE) == ScopeDomain.of_instance(
            ADAPTER,
            OWN_SECOND_INSTANCE,
        )

    async def test_read_only_tenant_member_cannot_configure_domains(self, db) -> None:
        """只读成员改不动域归属：那是一次数据可见性的改动，不是查看"""
        group = await make_group("acme-family", ACME_TENANT_ID)
        member = make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)

        with pytest.raises(HTTPException) as excinfo:
            await set_instance_domain(
                OWN_INSTANCE,
                InstanceDomainUpdateRequest(group_id=group.id),
                _current_user=member,
            )
        # 能力边界用 403（"你这个等级不能做写操作"），存在性问题才用 404
        assert excinfo.value.status_code == 403


class TestAccountMergeIsScopedToItsDomain:
    """账号合并：租户只能合并**自己域内**的账号，撤销同理"""

    async def test_tenant_cannot_merge_in_a_foreign_domain(self, db) -> None:
        body = MergeRequest(instance_key=FOREIGN_INSTANCE, alias_userid="1001", person_userid="1002")
        user = acme_admin()

        with pytest.raises(NotFoundError):
            await preview_identity_merge(body, _current_user=user)
        with pytest.raises(NotFoundError):
            await execute_identity_merge(body, _current_user=user)

        assert await DBIdentityAlias.all().count() == 0

    async def test_revoking_needs_the_alias_to_live_in_your_own_domain(self, db) -> None:
        """撤销按 `(域, 行 ID)` 双条件定位

        只按行 ID 删等于让任何一个能进后台的人撤销别的租户的合并 —— 而"撤销"在这里是
        一次会改分数的写操作（要把补进去的那一份扣回）。
        """
        foreign_domain = ScopeDomain.of_instance(ADAPTER, FOREIGN_INSTANCE)
        await merge_accounts(foreign_domain, person_uid="2001", alias_platform_userid="2002", operator="rival")
        alias = await DBIdentityAlias.get(domain_id=foreign_domain.domain_id, platform_userid="2002")

        with pytest.raises(NotFoundError):
            await revoke_identity_merge(alias.id, instance_key=OWN_INSTANCE, _current_user=acme_admin())

        assert await DBIdentityAlias.filter(id=alias.id).exists()

    async def test_alias_listing_is_confined_to_the_resolved_domain(self, db) -> None:
        own_domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        foreign_domain = ScopeDomain.of_instance(ADAPTER, FOREIGN_INSTANCE)
        await merge_accounts(own_domain, person_uid="1001", alias_platform_userid="1002", operator="acme")
        await merge_accounts(foreign_domain, person_uid="2001", alias_platform_userid="2002", operator="rival")

        response = await get_identity_aliases(instance_key=OWN_INSTANCE, _current_user=acme_admin())

        assert response.domain_id == own_domain.domain_id
        assert [(item.platform_userid, item.person_uid) for item in response.items] == [("1002", "1001")]

    async def test_merging_two_accounts_in_a_group_domain_uses_the_group_domain(self, db) -> None:
        """进组之后合并落在**组域**上，不是实例自成的那个域

        搞错这一维的后果是别名建在一个谁也不会去解析的域里 —— 合并"没反应"。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)

        await execute_identity_merge(
            MergeRequest(instance_key=OWN_INSTANCE, alias_userid="1002", person_userid="1001"),
            _current_user=user,
        )

        alias = await DBIdentityAlias.get(platform_userid="1002")
        assert alias.domain_id == ScopeDomain.of_group(created.group_id).domain_id


class TestMergePreviewMatchesTheRealMerge:
    """预览与执行必须给出同一个答案

    「取高的」这条规则在两个模块里各写了一份（预览在 `identity_admin`，执行在
    `affection/merge`）。本项目已经在"同一个判据的第二处实现"上反复栽跟头，
    所以这里直接对拍，而不是各测各的。
    """

    @pytest.mark.parametrize(
        ("person_scores", "alias_scores"),
        [
            # (人设 → 分数)：副号更高、主号更高、只有副号有、两边都没有
            ({1: 10}, {1: 40}),
            ({1: 70}, {1: 40}),
            ({}, {1: 25, 2: 5}),
            ({1: 10, 2: 30}, {}),
            ({1: 10, 2: 30}, {1: 10, 2: 90}),
        ],
    )
    async def test_preview_predicts_the_actual_transfers(self, db, person_scores, alias_scores) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        for preset_id, score in person_scores.items():
            await seed_affection(domain, platform_userid="1001", preset_id=preset_id, score=score)
        for preset_id, score in alias_scores.items():
            await seed_affection(domain, platform_userid="1002", preset_id=preset_id, score=score)

        body = MergeRequest(instance_key=OWN_INSTANCE, alias_userid="1002", person_userid="1001")
        preview = await preview_identity_merge(body, _current_user=acme_admin())
        predicted = {item.preset_id: (item.delta, item.score_after) for item in preview.transfers if item.delta > 0}

        result = await execute_identity_merge(body, _current_user=acme_admin())
        actual = {item.preset_id: (item.delta, item.score_after) for item in result.transfers}

        assert predicted == actual
        assert preview.total_delta == sum(delta for delta, _ in predicted.values())
        assert result.total_delta == preview.total_delta

    async def test_preview_reports_both_sides_without_touching_anything(self, db) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(domain, platform_userid="1001", preset_id=1, score=12, events=2)
        await seed_affection(domain, platform_userid="1002", preset_id=1, score=44, events=5)

        preview = await preview_identity_merge(
            MergeRequest(instance_key=OWN_INSTANCE, alias_userid="1002", person_userid="1001"),
            _current_user=acme_admin(),
        )

        assert (preview.person.best_score, preview.person.event_rows) == (12, 2)
        assert (preview.alias.best_score, preview.alias.event_rows) == (44, 5)
        assert preview.can_merge is True
        # 预览不能有副作用：既不建别名，也不提前把"取高的"补上去
        assert await DBIdentityAlias.all().count() == 0
        assert (await DBAffection.get(platform_userid="1001", preset_id=1)).score == 12
        assert (await DBAffection.get(platform_userid="1002", preset_id=1)).score == 44

    async def test_preview_blocks_what_the_service_would_reject(self, db) -> None:
        """预览里的 `blockers` 与执行的拒绝理由必须同时成立

        只有前者 = 前端置灰了一个其实能点的按钮；只有后者 = 用户点下去才知道不行。
        """
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await merge_accounts(domain, person_uid="1001", alias_platform_userid="1002", operator="acme")

        # 主账号自己已经是别名：合并会造出一条链，两侧都要拒
        body = MergeRequest(instance_key=OWN_INSTANCE, alias_userid="1003", person_userid="1002")
        preview = await preview_identity_merge(body, _current_user=acme_admin())
        assert preview.can_merge is False
        assert preview.blockers
        with pytest.raises(ValidationError):
            await execute_identity_merge(body, _current_user=acme_admin())

    async def test_preview_and_service_agree_on_an_already_merged_alias(self, db) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await merge_accounts(domain, person_uid="1001", alias_platform_userid="1002", operator="acme")

        body = MergeRequest(instance_key=OWN_INSTANCE, alias_userid="1002", person_userid="1009")
        preview = await preview_identity_merge(body, _current_user=acme_admin())
        assert preview.can_merge is False
        with pytest.raises(ValidationError):
            await execute_identity_merge(body, _current_user=acme_admin())

    async def test_undo_restores_the_alias_account(self, db) -> None:
        """撤销 = 删别名 + 扣回补偿；副账号自己那一行从来没被动过"""
        user = acme_admin()
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(domain, platform_userid="1001", preset_id=1, score=10)
        await seed_affection(domain, platform_userid="1002", preset_id=1, score=60)

        await execute_identity_merge(
            MergeRequest(instance_key=OWN_INSTANCE, alias_userid="1002", person_userid="1001"),
            _current_user=user,
        )
        alias = await DBIdentityAlias.get(domain_id=domain.domain_id, platform_userid="1002")
        assert (await DBAffection.get(platform_userid="1001", preset_id=1)).score == 60

        await revoke_identity_merge(alias.id, instance_key=OWN_INSTANCE, _current_user=user)

        assert await DBIdentityAlias.filter(id=alias.id).exists() is False
        assert (await DBAffection.get(platform_userid="1001", preset_id=1)).score == 10
        assert (await DBAffection.get(platform_userid="1002", preset_id=1)).score == 60


class TestPresetSwitchAsksBeforeItSwitches:
    """换人设的二次确认：不确认就拒绝，并把影响面写进理由里"""

    async def _channel(self, preset_id: Optional[int] = None) -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            channel_id="group_1",
            channel_type="group",
            channel_name="测试群",
            chat_key=f"{ADAPTER}-{OWN_INSTANCE}-group_1",
            preset_id=preset_id,
            data="{}",
            conversation_start_time=datetime.datetime.now(datetime.timezone.utc),
        )

    async def _preset(self, name: str, owner_instance_key: str = "") -> DBPreset:
        return await DBPreset.create(
            name=name,
            title=name,
            avatar="",
            content="c",
            description="",
            tags="",
            author="test",
            owner_instance_key=owner_instance_key,
        )

    async def test_unconfirmed_switch_is_rejected_with_the_head_count(self, db) -> None:
        first = await self._preset("甲")
        second = await self._preset("乙")
        channel = await self._channel(preset_id=first.id)
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        for platform_userid in ("1001", "1002", "1003"):
            await seed_affection(domain, platform_userid=platform_userid, preset_id=first.id, score=30)
        await seed_affection(domain, platform_userid="1001", preset_id=second.id, score=5)

        with pytest.raises(ValidationError) as excinfo:
            await set_chat_channel_preset(
                channel.chat_key,
                preset_id=second.id,
                _current_user=acme_admin(),
            )

        reason = str(excinfo.value)
        # 断的不只是"被拒了"，还有"理由里真的有那几个数" —— 没有数字的确认框等于没有确认框
        assert "3" in reason
        assert "甲" in reason and "乙" in reason
        assert (await DBChatChannel.get(id=channel.id)).preset_id == first.id

    async def test_impact_endpoint_returns_the_same_numbers(self, db) -> None:
        first = await self._preset("甲")
        second = await self._preset("乙")
        channel = await self._channel(preset_id=first.id)
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        for platform_userid in ("1001", "1002", "1003"):
            await seed_affection(domain, platform_userid=platform_userid, preset_id=first.id, score=30)
        await seed_affection(domain, platform_userid="1001", preset_id=second.id, score=5)

        impact = await get_chat_channel_preset_impact(
            channel.chat_key,
            preset_id=second.id,
            _current_user=acme_admin(),
        )

        assert impact.affected_people == 3
        assert impact.returning_people == 1
        assert impact.is_noop is False
        assert impact.current_preset_name == "甲"
        assert impact.target_preset_name == "乙"
        # 「旧数据保留、换回来就回来」必须跟着响应走，前端不各写一份措辞
        assert "换回去" in impact.reversible_note

    async def test_confirmed_switch_goes_through(self, db) -> None:
        first = await self._preset("甲")
        second = await self._preset("乙")
        channel = await self._channel(preset_id=first.id)

        result = await set_chat_channel_preset(
            channel.chat_key,
            preset_id=second.id,
            confirmed=True,
            _current_user=acme_admin(),
        )

        assert result.ok is True
        assert (await DBChatChannel.get(id=channel.id)).preset_id == second.id

    async def test_a_preset_owned_by_another_instance_is_a_400_not_a_500(self, db) -> None:
        """人设对本实例不可见时回 400，**不能**让模型层的 `ValueError` 冒成 500

        真部署上复现过：确认弹窗刚说完"本次影响 10 人"，`confirmed=true` 那一发就回一个
        没有原因的 500，同时往日志里写一条 ERROR + 完整调用栈。可见性判据本来就在模型层
        （`DBChatChannel.set_preset` 按 `owner_instance_key` 抛），路由只负责把它翻译成
        契约里的形状——所以这条用例钉的是"翻译发生了"，不是再判一次可见性。

        姊妹入口 `PUT /presets/instance/{key}/default` 走的是
        `visible_presets(scope)` + `NotFoundError`，本来就不会 500；两个入口的行为
        因此都落在 4xx 上。
        """
        mine = await self._preset("本实例可见")
        theirs = await self._preset("别家私有", owner_instance_key=OWN_SECOND_INSTANCE)
        channel = await self._channel(preset_id=mine.id)

        with pytest.raises(ValidationError) as excinfo:
            await set_chat_channel_preset(
                channel.chat_key,
                preset_id=theirs.id,
                confirmed=True,
                _current_user=acme_admin(),
            )

        # 理由要说得出是哪一个人设，否则运营看到的还是"换不了，不知道为什么"
        assert str(theirs.id) in str(excinfo.value)
        assert (await DBChatChannel.get(id=channel.id)).preset_id == mine.id

    async def test_a_switch_that_changes_nothing_is_not_blocked(self, db) -> None:
        """把实例默认人设显式绑到频道上是纯运维动作，生效人设没变

        拦它只会让运营学会一路点确认，而那正是二次确认失效的方式。
        """
        first = await self._preset("甲")
        instance = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=OWN_INSTANCE)
        instance.metadata_json = f'{{"{INSTANCE_METADATA_DEFAULT_PRESET_ID}": {first.id}}}'
        await instance.save()
        channel = await self._channel(preset_id=None)

        impact = await get_chat_channel_preset_impact(channel.chat_key, preset_id=first.id, _current_user=acme_admin())
        assert impact.is_noop is True

        result = await set_chat_channel_preset(channel.chat_key, preset_id=first.id, _current_user=acme_admin())
        assert result.ok is True
        assert (await DBChatChannel.get(id=channel.id)).preset_id == first.id

    async def test_instance_default_switch_also_asks(self, db) -> None:
        """第二个入口：改实例默认人设，一次性影响所有未绑人设的频道"""
        first = await self._preset("甲")
        second = await self._preset("乙")
        instance = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=OWN_INSTANCE)
        instance.metadata_json = f'{{"{INSTANCE_METADATA_DEFAULT_PRESET_ID}": {first.id}}}'
        await instance.save()
        await self._channel(preset_id=None)
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        for platform_userid in ("1001", "1002"):
            await seed_affection(domain, platform_userid=platform_userid, preset_id=first.id, score=30)

        impact = await get_instance_default_preset_impact(
            OWN_INSTANCE,
            preset_id=second.id,
            _current_user=acme_admin(),
        )
        assert (impact.affected_people, impact.affected_channels, impact.is_noop) == (2, 1, False)

        with pytest.raises(ValidationError):
            await set_instance_default_preset(
                OWN_INSTANCE,
                InstanceDefaultPresetUpdate(preset_id=second.id),
                _current_user=acme_admin(),
            )
        assert str(first.id) in (await DBAdapterInstance.get(id=instance.id)).metadata_json

        await set_instance_default_preset(
            OWN_INSTANCE,
            InstanceDefaultPresetUpdate(preset_id=second.id, confirmed=True),
            _current_user=acme_admin(),
        )
        assert str(second.id) in (await DBAdapterInstance.get(id=instance.id)).metadata_json

    async def test_instance_default_impact_resolves_a_dead_target_through_the_real_chain(self, db) -> None:
        """影响面里的"目标人设"必须**走一遍真回退链**，不能把传进来的 ID 直接当结果

        预览弹窗开着的时候人设被删掉是真会发生的（另一个人在人设页上点了删除）。
        真链会继续往下走、落到"无人设档"；直接回传进来的 ID 则会显示成
        「已删除的人设 #N」，而那不是切过去之后实际说话的那个人格 ——
        "预览说影响 3 人、实际换掉的是另一份"就是这么来的。
        """
        first = await self._preset("甲")
        instance = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=OWN_INSTANCE)
        instance.metadata_json = f'{{"{INSTANCE_METADATA_DEFAULT_PRESET_ID}": {first.id}}}'
        await instance.save()

        impact = await get_instance_default_preset_impact(
            OWN_INSTANCE,
            preset_id=987654,  # 库里没有这一行
            _current_user=acme_admin(),
        )

        # 真链：频道没绑 → 实例默认（探针里为 987654，查不到）→ 系统默认（未配）→ 内置默认 = 0
        assert impact.target_preset_id == 0
        assert impact.target_preset_name == "未绑定人设（内置默认）"
        assert impact.current_preset_id == first.id

    async def test_deleting_a_live_preset_leaves_a_trace(self, db, captured_warnings) -> None:
        """第四个入口：**删掉**一个正在用的人设，后果与手动换人设逐字相同

        绑着它的频道下一句话就落到回退链的下一档，那些频道的好感当场看起来归零，
        而挂在它下面的好感行原地变成孤儿。它长得不像"换人设"（没有目标人设可选），
        所以长期一点提示都没有 —— 这条用例钉住那条 WARNING 必然出现，且两个数都对得上。

        断言逐条都问过"修复不存在时会不会红"：摘掉 WARNING → `traces` 为空；
        把两个计数任意换一张表 → `1 个频道` / `3 行好感` 对不上。
        **不**断言"计数必须在 delete 之前"——那两列是普通整数列不是外键，
        删人设不会连带改它们，写成断言会恒绿（详见被测函数的 docstring）。
        """

        # 实例私有人设：全局共享人设对实例只读（`get_writable_preset` 判 403），
        # 删得掉的必然是这一种
        victim = await self._preset("要被删掉的", owner_instance_key=OWN_INSTANCE)
        await self._channel(preset_id=victim.id)
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        for platform_userid in ("1001", "1002", "1003"):
            await seed_affection(domain, platform_userid=platform_userid, preset_id=victim.id, score=30)

        await delete_preset(victim.id, instance_key=OWN_INSTANCE, _current_user=acme_admin())

        traces = [message for message in captured_warnings if "删除了正在使用的人设" in message]
        assert len(traces) == 1
        assert "1 个频道" in traces[0]
        assert "3 行好感" in traces[0]
        # 数据本身不级联删：人设可能是误删，孤儿数据原地保留，后台可见可手动清理
        assert await DBAffection.filter(preset_id=victim.id).count() == 3

    async def test_deleting_an_unused_preset_stays_quiet(self, db, captured_warnings) -> None:
        """反向对照：没人用的人设删掉不该刷告警，否则这条痕迹会淹在噪声里"""

        victim = await self._preset("没人用的", owner_instance_key=OWN_INSTANCE)

        await delete_preset(victim.id, instance_key=OWN_INSTANCE, _current_user=acme_admin())

        assert [message for message in captured_warnings if "删除了正在使用的人设" in message] == []


class TestOrphanDataIsVisibleAndNotCascaded:
    """人设删除后挂在它下面的数据保留成孤儿：后台可见、可手动清理、不级联删"""

    async def test_only_dead_presets_show_up(self, db) -> None:
        live = await DBPreset.create(
            name="活着的",
            title="活着的",
            avatar="",
            content="c",
            description="",
            tags="",
            author="t",
            owner_instance_key="",
        )
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(domain, platform_userid="1001", preset_id=live.id, score=10)
        await seed_affection(domain, platform_userid="1001", preset_id=999, score=20, events=3)
        await seed_affection(domain, platform_userid="1002", preset_id=999, score=30)
        # 无人设档（preset_id = 0）不是孤儿：它是"回落到内置默认人设"那一档
        await seed_affection(domain, platform_userid="1003", preset_id=0, score=1)

        response = await get_identity_orphans(instance_key=OWN_INSTANCE, _current_user=acme_admin())

        assert [(item.preset_id, item.affection_rows, item.people, item.event_rows) for item in response.items] == [
            (999, 2, 2, 3),
        ]
        assert "不级联删除" in response.policy_note

    async def test_orphans_of_an_instance_inside_a_group_are_still_listed(self, db) -> None:
        """实例进组之后，它的孤儿数据落在**组域**上（`instance_key` 是空串）

        只按实例列过滤就是一行都查不到，表现为「配了域之后孤儿清单整个空了」——
        与 `routers/affection.py:_scoped_query` 那个坑同一个形状，数据其实好好地在库里。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(
            OWN_INSTANCE,
            InstanceDomainUpdateRequest(group_id=created.group_id),
            _current_user=user,
        )
        group_domain = ScopeDomain.of_group(created.group_id)
        await seed_affection(group_domain, platform_userid="1001", preset_id=999, score=20, events=2)

        response = await get_identity_orphans(instance_key=OWN_INSTANCE, _current_user=user)

        assert [(item.preset_id, item.domain_id, item.affection_rows) for item in response.items] == [
            (999, f"g:{created.group_id}", 1),
        ]

    async def test_tenant_must_name_an_instance(self, db) -> None:
        """留空对租户不能退化成全局视图（与 `routers/affection.py` 同一形态）"""
        with pytest.raises(ValidationError):
            await get_identity_orphans(instance_key="", _current_user=acme_admin())

    async def test_tenant_cannot_read_a_foreign_instances_orphans(self, db) -> None:
        with pytest.raises(NotFoundError):
            await get_identity_orphans(instance_key=FOREIGN_INSTANCE, _current_user=acme_admin())

    async def test_cleanup_removes_only_the_named_bucket(self, db) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(domain, platform_userid="1001", preset_id=999, score=20, events=2)
        await seed_affection(domain, platform_userid="1001", preset_id=998, score=20, events=1)

        result = await cleanup_identity_orphans(
            OrphanCleanupRequest(instance_key=OWN_INSTANCE, preset_id=999, domain_id=domain.domain_id),
            _current_user=acme_admin(),
        )

        assert (result.affection, result.affection_event) == (1, 2)
        assert await DBAffection.filter(preset_id=999).count() == 0
        assert await DBAffection.filter(preset_id=998).count() == 1
        assert await DBAffectionEvent.filter(preset_id=998).count() == 1

    async def test_cleanup_refuses_when_the_preset_came_back(self, db) -> None:
        """预览与执行之间人设可能被重新建出来（云端人设导入会带回同一个 ID）

        那时候删的就不是孤儿了，所以执行前再确认一次。
        """
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        preset = await DBPreset.create(
            name="回来了",
            title="回来了",
            avatar="",
            content="c",
            description="",
            tags="",
            author="t",
            owner_instance_key="",
        )
        await seed_affection(domain, platform_userid="1001", preset_id=preset.id, score=20)

        with pytest.raises(NotFoundError):
            await cleanup_identity_orphans(
                OrphanCleanupRequest(instance_key=OWN_INSTANCE, preset_id=preset.id, domain_id=domain.domain_id),
                _current_user=acme_admin(),
            )
        assert await DBAffection.filter(preset_id=preset.id).count() == 1

    async def test_every_listed_bucket_is_actually_cleanable(self, db) -> None:
        """清单里列出来的每一桶都必须清得掉 —— 否则那一行是个死结

        一个实例在清单里合法地有**两种**桶：进组之前留在自成一域上的
        （`i:适配器:实例`）与进组之后落在组域上的（`g:组ID`）。判据一旦写成
        「请求里的 `domain_id` 必须等于本实例此刻的域」，前者就永远清不掉：
        清单里看得见、按钮永远报"域已经变了"、刷新之后还是它。而进组之前留下的那些行
        恰恰是**存量数据**里最需要能清掉的一类。
        """
        user = acme_admin()
        self_domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(self_domain, platform_userid="1001", preset_id=999, score=20, events=2)

        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)
        await seed_affection(ScopeDomain.of_group(created.group_id), platform_userid="2002", preset_id=999, score=5)

        listing = await get_identity_orphans(instance_key=OWN_INSTANCE, _current_user=user)
        assert sorted(item.domain_id for item in listing.items) == [
            f"g:{created.group_id}",
            self_domain.domain_id,
        ]

        for bucket in listing.items:
            await cleanup_identity_orphans(
                OrphanCleanupRequest(
                    instance_key=OWN_INSTANCE,
                    preset_id=bucket.preset_id,
                    domain_id=bucket.domain_id,
                ),
                _current_user=user,
            )
        assert await DBAffection.filter(preset_id=999).count() == 0
        assert await DBAffectionEvent.filter(preset_id=999).count() == 0

    async def test_cleanup_touches_only_the_requested_bucket(self, db) -> None:
        """同一个人设在同一个作用域下有**两桶**时，`domain_id` 是选哪一桶，不是装饰

        实例进组前后各留了一批挂在同一个已删人设下的数据，两桶的 `preset_id` 相同。
        只按人设匹配就会去清"排在前面的那一桶"——操作者点的是 A，删掉的是 B，
        而这一步不可撤销。
        """
        user = acme_admin()
        self_domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(self_domain, platform_userid="1001", preset_id=999, score=20, events=2)

        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)
        group_domain = ScopeDomain.of_group(created.group_id)
        await seed_affection(group_domain, platform_userid="2002", preset_id=999, score=5, events=3)

        result = await cleanup_identity_orphans(
            OrphanCleanupRequest(
                instance_key=OWN_INSTANCE,
                preset_id=999,
                domain_id=self_domain.domain_id,
            ),
            _current_user=user,
        )

        assert (result.affection, result.affection_event) == (1, 2)
        # 被点名的那一桶没了
        assert await DBAffection.filter(**self_domain.as_columns(), preset_id=999).count() == 0
        # 另一桶一行都没少
        assert await DBAffection.filter(**group_domain.as_columns(), preset_id=999).count() == 1
        assert await DBAffectionEvent.filter(**group_domain.as_columns(), preset_id=999).count() == 3

    async def test_cleanup_refuses_a_bucket_that_left_this_instances_scope(self, db) -> None:
        """预览与执行之间实例被移出了组：那个组的桶不再属于本作用域 —— 404，不去猜

        这是"陈旧的预览"真正危险的方向。上面那条管的是"两种桶都得能清"，
        这条管的是"不在清单里的桶一律够不着"，两条缺一不可。
        """
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="acme-family"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)
        group_domain = ScopeDomain.of_group(created.group_id)
        await seed_affection(group_domain, platform_userid="1001", preset_id=999, score=20)

        # 预览之后、执行之前，实例回到了自成一域
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=None), _current_user=user)

        with pytest.raises(NotFoundError):
            await cleanup_identity_orphans(
                OrphanCleanupRequest(instance_key=OWN_INSTANCE, preset_id=999, domain_id=group_domain.domain_id),
                _current_user=user,
            )
        assert await DBAffection.filter(preset_id=999).count() == 1

    async def test_cleanup_refuses_a_foreign_domain_id(self, db) -> None:
        """`domain_id` 是"选哪一桶"，不是一个可以随手编的作用域

        编一个别的租户的组域进来，服务端此刻重新解析出来的清单里没有它 —— 404。
        """
        user = acme_admin()
        rival_group = await make_group("rival-family", RIVAL_TENANT_ID)
        rival_domain = ScopeDomain.of_group(rival_group.id)
        await seed_affection(rival_domain, platform_userid="1001", preset_id=999, score=20)

        with pytest.raises(NotFoundError):
            await cleanup_identity_orphans(
                OrphanCleanupRequest(instance_key=OWN_INSTANCE, preset_id=999, domain_id=rival_domain.domain_id),
                _current_user=user,
            )
        assert await DBAffection.filter(preset_id=999).count() == 1


class TestPluginPresetSwitchAlwaysLeavesATrace:
    """插件/沙盒换人设那条路挂不上确认框，那它必须**必然留痕**

    `AgentCtx.set_preset` 的调用者是插件代码，那里没有人可确认。禁掉它会打死一整类
    合法用法（剧情插件按情节切人格是产品功能），所以那条路选的是"播报"：
    换掉生效人设时打 WARNING，带上影响人数。没有这条用例，那段 WARNING 摘掉之后
    整套端点用例照样全绿 —— 而运营就只剩"一群人第二天来问我的好感呢"。
    """

    async def _channel(self, preset_id: Optional[int] = None) -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            channel_id="group_9",
            channel_type="group",
            channel_name="剧情群",
            chat_key=f"{ADAPTER}-{OWN_INSTANCE}-group_9",
            preset_id=preset_id,
            data="{}",
            conversation_start_time=datetime.datetime.now(datetime.timezone.utc),
        )

    async def test_switching_through_the_plugin_path_logs_the_head_count(self, db, captured_warnings) -> None:
        from nekro_agent.schemas.agent_ctx import AgentCtx

        first = await DBPreset.create(
            name="甲", title="甲", avatar="", content="c", description="", tags="", author="t", owner_instance_key="",
        )
        second = await DBPreset.create(
            name="乙", title="乙", avatar="", content="c", description="", tags="", author="t", owner_instance_key="",
        )
        channel = await self._channel(preset_id=first.id)
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        for platform_userid in ("1001", "1002"):
            await seed_affection(domain, platform_userid=platform_userid, preset_id=first.id, score=30)

        ctx = AgentCtx(from_chat_key=channel.chat_key)
        ctx._db_chat_channel = channel  # noqa: SLF001  测试替身：真频道行，只是省一次查询
        changed = await ctx.set_preset(second.id)

        assert changed is True
        assert (await DBChatChannel.get(id=channel.id)).preset_id == second.id
        traces = [message for message in captured_warnings if "插件/沙盒换了频道人设" in message]
        assert len(traces) == 1
        assert "当前人设下有 2 人的好感记录" in traces[0]

    async def test_a_plugin_switch_that_changes_nothing_stays_quiet(self, db, captured_warnings) -> None:
        """反向对照：生效人设没变时不该刷告警，否则真正的那一条会淹掉"""
        from nekro_agent.schemas.agent_ctx import AgentCtx

        first = await DBPreset.create(
            name="甲", title="甲", avatar="", content="c", description="", tags="", author="t", owner_instance_key="",
        )
        instance = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=OWN_INSTANCE)
        instance.metadata_json = f'{{"{INSTANCE_METADATA_DEFAULT_PRESET_ID}": {first.id}}}'
        await instance.save()
        channel = await self._channel(preset_id=None)

        ctx = AgentCtx(from_chat_key=channel.chat_key)
        ctx._db_chat_channel = channel  # noqa: SLF001
        # 把实例默认人设显式绑到频道上：纯运维动作，说话的人格一点没变
        changed = await ctx.set_preset(first.id)

        assert changed is True
        assert [message for message in captured_warnings if "插件/沙盒换了频道人设" in message] == []


class TestAffectionTimelineNamesTheAccountThatDidIt:
    """时间线是按**人**取的并集，每条必须说得出是哪个号干的

    合并过的人身上会同时出现几个号的事。库里 `source_platform_userid` 存空串表示
    "与人相同"（存量行与没合并过的人都是这个取值），出参必须还原成具体账号 ——
    丢掉这条回落，没合并过的人整条时间线的来源账号都是空的，
    界面上那一栏要么空白要么得在前端再写一遍这条规则。
    """

    async def test_legacy_rows_report_the_person_as_the_source_account(self, db) -> None:
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        record = await DBAffection.create(
            **domain.as_columns(),
            platform_userid="1001",
            preset_id=0,
            nickname="nick",
            score=10,
            stage="stranger",
        )
        # 存量行的形态：`source_platform_userid` 是空串
        await DBAffectionEvent.create(
            **domain.as_columns(),
            platform_userid="1001",
            preset_id=0,
            source_platform_userid="",
            delta=3,
            score_after=10,
            source="llm",
            reason="聊了一会儿",
        )

        detail = await get_affection_detail(record.id, instance_key=OWN_INSTANCE, _current_user=acme_admin())

        assert [event.source_platform_userid for event in detail.events] == ["1001"]



class TestOrphanCleanupServiceGuardStandsOnItsOwn:
    """服务层那道「人设是不是又回来了」的复查，必须有一条**直接**打到它的用例

    路由那一层已经先按 `list_orphan_buckets` 过滤过一遍（活着的人设根本不成桶），
    所以从路由进来的用例永远走不到这道门 —— 摘掉它，整套端点用例照样全绿。
    这正是本项目反复栽的那个形态：一处判据被上游挡着，于是它自己没有守卫。

    它守的是**真实存在的时间窗**：预览与执行之间人设可能被重新建出来
    （云端人设导入会带回同一个 ID），那一刻要删的就不是孤儿数据了。
    """

    async def test_cleanup_refuses_when_the_preset_reappeared_between_listing_and_delete(self, db) -> None:
        from nekro_agent.services.identity_admin import (
            IdentityAdminRejected,
            cleanup_orphan_preset,
            list_orphan_buckets,
        )

        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(domain, platform_userid="1001", preset_id=4242, score=20, events=2)

        # 列清单的那一刻它确实是孤儿
        buckets = [item for item in await list_orphan_buckets([OWN_INSTANCE]) if item.preset_id == 4242]
        assert len(buckets) == 1

        # 执行之前人设被重新导入回来（同一个 ID）
        preset = await DBPreset.create(
            id=4242,
            name="导入回来的",
            title="导入回来的",
            avatar="",
            content="c",
            description="",
            tags="",
            author="cloud",
            owner_instance_key="",
        )
        assert preset.id == 4242

        with pytest.raises(IdentityAdminRejected):
            await cleanup_orphan_preset(buckets[0])
        assert await DBAffection.filter(preset_id=4242).count() == 1
        assert await DBAffectionEvent.filter(preset_id=4242).count() == 2

    async def test_the_same_bucket_is_deleted_once_the_preset_is_gone_again(self, db) -> None:
        """正向对照：这道门不是恒抛 —— 人设确实不在时同一桶必须删得掉"""
        from nekro_agent.services.identity_admin import cleanup_orphan_preset, list_orphan_buckets

        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        await seed_affection(domain, platform_userid="1001", preset_id=4242, score=20, events=2)
        buckets = [item for item in await list_orphan_buckets([OWN_INSTANCE]) if item.preset_id == 4242]

        counts = await cleanup_orphan_preset(buckets[0])

        assert counts == {"affection": 1, "affection_event": 2}
        assert await DBAffection.filter(preset_id=4242).count() == 0



class TestOperatorFacingCopyIsNotDeveloperNotes:
    """随响应下发、界面**原样显示**的那几段说明，不能带标记语言和源码路径

    这几段刻意由后端给、前端不复算一份（同一件事在两个页面上说成两种措辞会被当成
    两种后果）。代价是它们从后端的注释风格里长出来：反引号、`**加粗**`、
    `services/affection/merge.py` 这种路径。MUI 的 Alert 不渲染 markdown，
    这些东西在界面上就是字面的星号、反引号和一串谁也点不开的文件名 ——
    而这一页租户管理员也看得到。
    """

    def test_notes_render_as_plain_sentences(self) -> None:
        import re

        from nekro_agent.routers.console.identity import ORPHAN_POLICY_NOTE
        from nekro_agent.services.identity_admin import (
            MEMORY_SCOPE_NOTE,
            MERGE_REVERSIBLE_NOTE,
            PRESET_SWITCH_REVERSIBLE_NOTE,
        )

        for name, text in [
            ("MEMORY_SCOPE_NOTE", MEMORY_SCOPE_NOTE),
            ("MERGE_REVERSIBLE_NOTE", MERGE_REVERSIBLE_NOTE),
            ("PRESET_SWITCH_REVERSIBLE_NOTE", PRESET_SWITCH_REVERSIBLE_NOTE),
            ("ORPHAN_POLICY_NOTE", ORPHAN_POLICY_NOTE),
        ]:
            assert "`" not in text, name
            assert "**" not in text, name
            assert not re.search(r"\w+/[\w/]+\.py", text), name


class TestAffectionDetailSaysWhoseAffectionItIs:
    """好感度详情页标注「这份好感属于：域 X / 人设 甲」"""

    async def test_self_domain_and_live_preset(self, db) -> None:
        preset = await DBPreset.create(
            name="甲",
            title="甲",
            avatar="",
            content="c",
            description="",
            tags="",
            author="t",
            owner_instance_key="",
        )
        domain = ScopeDomain.of_instance(ADAPTER, OWN_INSTANCE)
        record = await seed_affection(domain, platform_userid="1001", preset_id=preset.id, score=42)

        detail = await get_affection_detail(record.id, instance_key=OWN_INSTANCE, _current_user=acme_admin())

        assert detail.scope.domain_id == f"i:{ADAPTER}:{OWN_INSTANCE}"
        assert OWN_INSTANCE in detail.scope.domain_label
        assert detail.scope.is_group_domain is False
        assert (detail.scope.preset_name, detail.scope.preset_exists) == ("甲", True)
        assert detail.item.domain_id == detail.scope.domain_id
        assert detail.item.preset_id == preset.id

    async def test_group_domain_and_dead_preset_and_aliases(self, db) -> None:
        """三件事一起验：组域的标签、已删人设标成孤儿、合并进来的副账号列出来"""
        user = acme_admin()
        created = await create_identity_domain(DomainCreateRequest(name="家族"), _current_user=user)
        await set_instance_domain(OWN_INSTANCE, InstanceDomainUpdateRequest(group_id=created.group_id), _current_user=user)
        domain = ScopeDomain.of_group(created.group_id)
        record = await seed_affection(domain, platform_userid="1001", preset_id=777, score=8)
        await merge_accounts(domain, person_uid="1001", alias_platform_userid="1002", operator="acme")

        detail = await get_affection_detail(record.id, instance_key=OWN_INSTANCE, _current_user=user)

        assert detail.scope.domain_id == f"g:{created.group_id}"
        assert "家族" in detail.scope.domain_label
        assert detail.scope.is_group_domain is True
        assert detail.scope.preset_exists is False
        assert "777" in detail.scope.preset_name
        assert detail.scope.alias_accounts == ["1002"]
