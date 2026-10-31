"""账号合并自助申报（`self_report` 链路）的行为契约

「后台手动为主、Bot 内自助申报为辅」——本文件守的是后者：

1. **申报只留单子，一个数据行都不碰**：`submit` 不写别名、不碰好感；
   兑现只发生在管理员批准那一刻，且走与手动合并完全相同的 `merge_accounts`。
2. **闸门**：一人一域一张 pending；一个账号一域只被一张 pending 指着；
   目标已被合并（无论并入谁）都拒在提交时。
3. **批准时的两道复核**：单子必须还是 pending；申报人的「人」标识不能中途漂移
   （漂移说明签发时的语义已变，拒掉重新申报）。
4. **域隔离**：申报单按 `(域, 单号)` 双条件定位——跨域审批拿到的是"不存在"，
   与 `revoke_identity_merge` 同一条理由。
5. **通知尽力而为**：审批结果回申报时的会话，发送失败不滚审批（适配器缺席的
   测试环境本身就是这条用例）。
"""

import datetime
from typing import List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_identity import (
    ALIAS_REQUEST_APPROVED,
    ALIAS_REQUEST_PENDING,
    ALIAS_REQUEST_REJECTED,
    ALIAS_SOURCE_SELF_REPORT,
    DBIdentityAlias,
    DBIdentityAliasRequest,
)
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.identity import (
    AliasRequestReviewBody,
    approve_identity_alias_request,
    list_identity_alias_requests,
    reject_identity_alias_request,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.merge import merge_accounts
from nekro_agent.services.identity_requests import (
    AliasRequestError,
    approve_alias_request,
    cancel_alias_request,
    list_requests_by_reporter,
    reject_alias_request,
    submit_alias_request,
)
from nekro_agent.services.identity_scope import ScopeDomain, resolve_memory_domain
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"

INSTANCE_LAYOUT: List[Tuple[str, Optional[int]]] = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, RIVAL_TENANT_ID),
]

CHAT_KEY = f"{ADAPTER}-{OWN_INSTANCE}-group_777"
FOREIGN_CHAT_KEY = f"{ADAPTER}-{FOREIGN_INSTANCE}-group_888"

REPORTER = "user-new"
CLAIMED = "user-old"


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
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            channel_id="group_777",
            channel_type="group",
            chat_key=CHAT_KEY,
            data="{}",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=FOREIGN_INSTANCE,
            channel_id="group_888",
            channel_type="group",
            chat_key=FOREIGN_CHAT_KEY,
            data="{}",
        )
        yield
    finally:
        await Tortoise.close_connections()


async def own_domain() -> ScopeDomain:
    return await resolve_memory_domain(ADAPTER, OWN_INSTANCE)


async def foreign_domain() -> ScopeDomain:
    return await resolve_memory_domain(ADAPTER, FOREIGN_INSTANCE)


async def seed_affection(
    domain: ScopeDomain,
    *,
    platform_userid: str,
    preset_id: int,
    score: int,
) -> DBAffection:
    return await DBAffection.create(
        **domain.as_columns(),
        platform_userid=platform_userid,
        preset_id=preset_id,
        nickname=f"nick-{platform_userid}",
        score=score,
        stage="stranger",
    )


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
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


async def submit(domain: Optional[ScopeDomain] = None, *, claimed: str = CLAIMED, reporter: str = REPORTER):
    return await submit_alias_request(
        domain or await own_domain(),
        reporter_userid=reporter,
        reporter_chat_key=CHAT_KEY,
        claimed_userid=claimed,
        note="旧号",
    )


@pytest.mark.usefixtures("db")
class TestSubmit:
    """申报侧闸门：一个数据行都不碰，只留一张 pending"""

    async def test_happy_path(self) -> None:
        domain = await own_domain()
        request = await submit(domain)

        assert request.status == ALIAS_REQUEST_PENDING
        assert request.domain_id == domain.domain_id
        assert request.alias_platform_userid == CLAIMED
        assert request.reporter_userid == REPORTER
        # 申报人没合并过时，「人」标识就是账号自己
        assert request.reporter_person_uid == REPORTER
        assert request.reporter_chat_key == CHAT_KEY
        # 一个数据行都不碰：别名表仍为空
        assert await DBIdentityAlias.all().count() == 0

    async def test_reject_self_claim(self) -> None:
        with pytest.raises(AliasRequestError):
            await submit(claimed=REPORTER)

    async def test_one_pending_per_reporter(self) -> None:
        await submit()
        with pytest.raises(AliasRequestError, match="待审批"):
            await submit(claimed="another-old")

    async def test_claimed_account_only_one_pending(self) -> None:
        await submit()
        with pytest.raises(AliasRequestError, match="待审批"):
            await submit(reporter="someone-else")

    async def test_claimed_already_merged_to_reporter(self) -> None:
        domain = await own_domain()
        await merge_accounts(domain, person_uid=REPORTER, alias_platform_userid=CLAIMED)
        with pytest.raises(AliasRequestError, match="已经并入"):
            await submit(domain)

    async def test_claimed_already_merged_to_other(self) -> None:
        domain = await own_domain()
        await merge_accounts(domain, person_uid="other-person", alias_platform_userid=CLAIMED)
        with pytest.raises(AliasRequestError, match="其他人"):
            await submit(domain)


@pytest.mark.usefixtures("db")
class TestCancelAndList:
    async def test_cancel_own_pending(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        canceled = await cancel_alias_request(domain, REPORTER)
        assert canceled.id == request.id
        assert canceled.status == "canceled"
        # 撤回后可以再申报
        await submit(domain, claimed="another-old")

    async def test_cancel_without_pending_fails(self) -> None:
        with pytest.raises(AliasRequestError):
            await cancel_alias_request(await own_domain(), REPORTER)

    async def test_list_by_reporter(self) -> None:
        domain = await own_domain()
        await submit(domain)
        mine = await list_requests_by_reporter(domain, REPORTER)
        assert [item.id for item in mine] == [1]
        assert await list_requests_by_reporter(domain, "nobody") == []


@pytest.mark.usefixtures("db")
class TestApprove:
    """批准 = 手动合并的同一条路径 + 申报单自己的两道复核"""

    async def test_approve_creates_self_report_alias(self) -> None:
        domain = await own_domain()
        await seed_affection(domain, platform_userid=CLAIMED, preset_id=1, score=80)
        await seed_affection(domain, platform_userid=REPORTER, preset_id=1, score=30)
        request = await submit(domain)

        updated, result = await approve_alias_request(domain, request.id, operator="admin")

        assert updated.status == ALIAS_REQUEST_APPROVED
        assert updated.reviewed_by == "admin"
        assert updated.reviewed_at is not None
        # 兑现成别名，来源是自助申报；补偿「取高的」把 80 补到主账号
        alias = await DBIdentityAlias.get_or_none(domain_id=domain.domain_id, platform_userid=CLAIMED)
        assert alias is not None
        assert alias.person_uid == REPORTER
        assert alias.source == ALIAS_SOURCE_SELF_REPORT
        assert result.total_delta == 50
        person_row = await DBAffection.get_or_none(
            **domain.as_columns(), platform_userid=REPORTER, preset_id=1
        )
        assert person_row is not None and person_row.score == 80

    async def test_approve_is_idempotent_guarded(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        await approve_alias_request(domain, request.id, operator="admin")
        with pytest.raises(AliasRequestError, match="已经审过"):
            await approve_alias_request(domain, request.id, operator="admin")

    async def test_approve_rejects_person_drift(self) -> None:
        """提交后申报人自己被并进了别人：签发时的兑现方向已经变了"""
        domain = await own_domain()
        request = await submit(domain)
        await merge_accounts(domain, person_uid="boss", alias_platform_userid=REPORTER)
        with pytest.raises(AliasRequestError, match="归属已变化"):
            await approve_alias_request(domain, request.id, operator="admin")
        assert await DBIdentityAlias.filter(platform_userid=CLAIMED).count() == 0

    async def test_approve_rejects_when_claimed_merged_elsewhere(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        await merge_accounts(domain, person_uid="other-person", alias_platform_userid=CLAIMED)
        with pytest.raises(AliasRequestError):
            await approve_alias_request(domain, request.id, operator="admin")

    async def test_reporter_who_is_alias_still_works(self) -> None:
        """申报人自己已是别人的副号时，兑现目标是解析出的「人」，不是裸账号"""
        domain = await own_domain()
        await merge_accounts(domain, person_uid="boss", alias_platform_userid=REPORTER)
        request = await submit(domain)
        assert request.reporter_person_uid == "boss"
        _request, result = await approve_alias_request(domain, request.id, operator="admin")
        assert result.person_uid == "boss"

    async def test_cross_domain_request_not_found(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        with pytest.raises(AliasRequestError):
            await approve_alias_request(await foreign_domain(), request.id, operator="admin")


@pytest.mark.usefixtures("db")
class TestReject:
    async def test_reject_leaves_trace_and_no_alias(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        rejected = await reject_alias_request(domain, request.id, operator="admin", note="信息不足")

        assert rejected.status == ALIAS_REQUEST_REJECTED
        assert rejected.reviewed_by == "admin"
        assert rejected.review_note == "信息不足"
        assert rejected.reviewed_at is not None
        assert await DBIdentityAlias.all().count() == 0
        # 拒绝后同一账号可以重新申报
        await submit(domain)

    async def test_reject_twice_fails(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        await reject_alias_request(domain, request.id, operator="admin")
        with pytest.raises(AliasRequestError, match="已经审过"):
            await reject_alias_request(domain, request.id, operator="admin")


@pytest.mark.usefixtures("db")
class TestConsoleApi:
    """路由层：作用域用"够得着的实例"表达，跨域审批拿到 404"""

    async def test_list_and_approve(self) -> None:
        domain = await own_domain()
        request = await submit(domain)

        listing = await list_identity_alias_requests(
            instance_key=OWN_INSTANCE, status=ALIAS_REQUEST_PENDING, _current_user=acme_admin()
        )
        assert listing.domain_id == domain.domain_id
        assert [item.id for item in listing.items] == [request.id]
        assert listing.items[0].reporter_userid == REPORTER

        response = await approve_identity_alias_request(
            request.id,
            AliasRequestReviewBody(instance_key=OWN_INSTANCE, note="已核实"),
            _current_user=acme_admin(),
        )
        assert response.request.status == ALIAS_REQUEST_APPROVED
        assert response.merge.alias_platform_userid == CLAIMED
        assert response.merge.person_uid == REPORTER

    async def test_reject_via_route(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        item = await reject_identity_alias_request(
            request.id,
            AliasRequestReviewBody(instance_key=OWN_INSTANCE, note="不像同一个人"),
            _current_user=acme_admin(),
        )
        assert item.status == ALIAS_REQUEST_REJECTED
        assert item.review_note == "不像同一个人"

    async def test_foreign_request_is_404(self) -> None:
        """用本租户的实例去拿别域的单子：与"不存在"不可区分"""
        foreign = await foreign_domain()
        request = await submit_alias_request(
            foreign,
            reporter_userid="rival-user",
            reporter_chat_key=FOREIGN_CHAT_KEY,
            claimed_userid="rival-old",
        )
        with pytest.raises(NotFoundError):
            await approve_identity_alias_request(
                request.id,
                AliasRequestReviewBody(instance_key=OWN_INSTANCE),
                _current_user=acme_admin(),
            )

    async def test_foreign_instance_key_is_404(self) -> None:
        """用别人租户的实例当作用域：实例归属那道门先挡"""
        request = await submit()
        with pytest.raises(NotFoundError):
            await approve_identity_alias_request(
                request.id,
                AliasRequestReviewBody(instance_key=FOREIGN_INSTANCE),
                _current_user=acme_admin(),
            )
        # 对端租户自己也批不了 acme 域里的单子
        with pytest.raises(NotFoundError):
            await reject_identity_alias_request(
                request.id,
                AliasRequestReviewBody(instance_key=FOREIGN_INSTANCE),
                _current_user=rival_admin(),
            )

    async def test_list_requires_own_instance(self) -> None:
        with pytest.raises(NotFoundError):
            await list_identity_alias_requests(
                instance_key=FOREIGN_INSTANCE, status=ALIAS_REQUEST_PENDING, _current_user=acme_admin()
            )

    async def test_approve_nonpending_via_route_is_400(self) -> None:
        domain = await own_domain()
        request = await submit(domain)
        await approve_identity_alias_request(
            request.id,
            AliasRequestReviewBody(instance_key=OWN_INSTANCE),
            _current_user=acme_admin(),
        )
        with pytest.raises(ValidationError):
            await reject_identity_alias_request(
                request.id,
                AliasRequestReviewBody(instance_key=OWN_INSTANCE),
                _current_user=acme_admin(),
            )


@pytest.mark.usefixtures("db")
class TestBotCommand:
    """`merge_request` 命令：申报/查询/撤回三个入口都落在同一个域上"""

    @staticmethod
    def _context(user_id: str = REPORTER, chat_key: str = CHAT_KEY):
        from nekro_agent.services.command.schemas import CommandExecutionContext

        return CommandExecutionContext(
            user_id=user_id,
            chat_key=chat_key,
            username=user_id,
            adapter_key=ADAPTER,
        )

    @staticmethod
    def _command():
        from nekro_agent.services.command.built_in.identity_cmd import MergeRequestCommand

        return MergeRequestCommand()

    async def test_submit_via_command(self) -> None:
        response = await self._command().execute(self._context(), f"{CLAIMED} 换手机了")
        assert response.status.value == "success"
        request = await DBIdentityAliasRequest.get_or_none(alias_platform_userid=CLAIMED)
        assert request is not None and request.status == ALIAS_REQUEST_PENDING

    async def test_list_and_cancel_via_command(self) -> None:
        command = self._command()
        assert (await command.execute(self._context(), "")).status.value == "success"  # 空清单
        await command.execute(self._context(), CLAIMED)
        listing = await command.execute(self._context(), "")
        assert CLAIMED in listing.message
        canceled = await command.execute(self._context(), "cancel")
        assert canceled.status.value == "success"
        request = await DBIdentityAliasRequest.get_or_none(alias_platform_userid=CLAIMED)
        assert request is not None and request.status == "canceled"

    async def test_unknown_channel_fails(self) -> None:
        response = await self._command().execute(self._context(chat_key="ghost-channel"), CLAIMED)
        assert response.status.value == "error"

    async def test_domain_follows_the_chat(self) -> None:
        """在哪个会话里申报，单子就属于那个会话的域"""
        await self._command().execute(self._context(chat_key=FOREIGN_CHAT_KEY, user_id="rival-user"), "rival-old")
        request = await DBIdentityAliasRequest.get_or_none(alias_platform_userid="rival-old")
        assert request is not None
        assert request.domain_id == (await foreign_domain()).domain_id
