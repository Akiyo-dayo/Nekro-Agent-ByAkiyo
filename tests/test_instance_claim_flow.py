"""待认领协议端的认领流程，以及「没配」与「掉了」的状态语义

现场形态（服务器实测）：管理员在总后台建了 `inst3`（显示名 Tsubaki）、接了一个真实的
NapCat。协议端连上时 `resolve_instance_key()` 把自己登记成 `ob{QQ号}` 并把流量与全部频道
归给了它，而 `inst3` 是另一行、`provider_account_id` 为空、永远拿不到那条连接。于是：

- 实例管理里 `inst3` 一直显示"离线"，管理员据此去查协议端 —— 协议端一切正常；
- 管理域切到 `inst3` 之后频道管理是空的 —— 会话全在 `ob{QQ号}` 名下。

本文件钉住三件事：

**「没配」与「掉了」是两个状态**
`unbound`（从未绑定账号）不是 `offline`。分开之后掉线监测不再对它反复告警——它从来没上线过，
那条告警从第一次起就是错的。存量里挂着 offline 的空壳行由一次性改判摘出来，判据与读侧推导相同。

**认领有两条出路，都走既有的归属迁移**
「认领为新实例」= 建一个用管理员想要的标识的实例 + 把协议端整体交给它；
「并入已有实例」= 目标是既有实例。两条出路是同一段代码，搬运直接复用
`chat_key_migration.migrate_instance_ownership()`，不重写迁移。

**半成品必须可重试**
有频道搬失败时账号不过户、源实例不归档，界面上仍是「有一个未认领的协议端」，重跑即可补齐；
而不是留下「账号已经过户、历史却少了一半」——那种状态没有任何入口能修回来。
"""

import datetime
import json
from typing import Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.instances import (
    adopt_console_instance,
    claim_console_instance,
    get_console_unclaimed_instances,
)
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.console import (
    InstanceAdoptRequest,
    InstanceClaimRequest,
    InstanceCreateRequest,
    InstanceProvider,
    InstanceStatus,
    InstanceUpdateRequest,
)
from nekro_agent.schemas.errors import ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.console import instance_health as health_module
from nekro_agent.services.console.instance_health import UNMANAGED_STATUSES, InstanceHealthService
from nekro_agent.services.console.instance_manager import (
    STATUS_UNBOUND,
    adopt_unclaimed_instance,
    claim_unclaimed_as_new_instance,
    create_instance,
    describe_instance,
    list_adopt_targets,
    list_unclaimed_protocol_ends,
    normalize_unbound_instance_status,
    resolve_instance_status,
    set_instance_enabled,
    update_instance,
)
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
ACCOUNT = "3000000001"
# 协议端自行连入时由 `resolve_instance_key()` 生成的标识
AUTO = "ob3000000001"
# 管理员手工建的空壳实例（现场就叫 inst3，显示名 Tsubaki）
SHELL = "inst3"
# 管理员认领时想要的标识
WANTED = "tsubaki"
GROUP = "group_500000001"
OTHER_GROUP = "group_100"

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2


@pytest.fixture(autouse=True)
def offline_qdrant(monkeypatch: pytest.MonkeyPatch):
    """向量库不参与用例：段落 payload 同步在真实部署里走网络，这里按"全部同步成功"处理

    保留调用而不是绕过它，是为了让「改了段落行就必须同步向量」这条链路仍然被执行到。
    """
    from nekro_agent.services.memory import qdrant_manager as qdrant_module

    async def fake_set(paragraph_ids: List[int], instance_key: str) -> int:  # noqa: ARG001
        return len(paragraph_ids)

    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "set_paragraph_instance_key", fake_set)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    from nekro_agent.adapters.onebot_v11.core import instance as instance_module

    instance_module.clear_instance_cache()
    try:
        yield
    finally:
        instance_module.clear_instance_cache()
        await Tortoise.close_connections()


async def seed_instance(
    instance_key: str,
    *,
    account: str = "",
    status: str = "offline",
    provider: str = "onebot_v11",
    enabled: bool = True,
    tenant_id: Optional[int] = None,
    adapter_key: str = ADAPTER,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        provider=provider,
        provider_account_id=account,
        status=status,
        enabled=enabled,
        tenant_id=tenant_id,
    )


async def seed_channel(instance_key: str, channel_id: str = GROUP, channel_name: str = "远航的阿列夫一") -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_type=ChatType.GROUP.value,
        channel_name=channel_name,
        chat_key=f"{ADAPTER}-{instance_key}-{channel_id}",
        is_active=True,
        observe_mode=False,
        data=json.dumps({}),
    )


async def seed_message(chat_key: str, instance_key: str, index: int) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id="1001",
        sender_name="用户",
        sender_nickname="用户",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=f"{chat_key}-{index}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid="1001",
        content_text="历史消息",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000_000 + index,
    )


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；权限判定只读这三个字段"""
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


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


async def seed_field_layout() -> None:
    """复现服务器上的现场：真正在线的是自动登记的那一行，管理员的实例是个空壳"""
    await seed_instance(AUTO, account=ACCOUNT, status="unclaimed")
    await seed_instance(SHELL, account="", status="offline")
    await seed_channel(AUTO, GROUP)
    await seed_channel(AUTO, OTHER_GROUP, channel_name="另一个群")
    await seed_message(f"{ADAPTER}-{AUTO}-{GROUP}", AUTO, 1)
    await seed_message(f"{ADAPTER}-{AUTO}-{GROUP}", AUTO, 2)


# ---------------------------------------------------------------------------
# 1. 状态语义：「没配」不是「掉了」
# ---------------------------------------------------------------------------


class TestUnboundIsNotOffline:
    """未绑定账号有自己的状态，不再挤在 offline 里"""

    @pytest.mark.parametrize(
        ("raw_status", "provider", "account", "expected"),
        [
            # 现场那一行：管理员建的空壳实例，从来没绑过账号
            pytest.param("offline", "builtin_napcat", "", InstanceStatus.UNBOUND, id="空壳实例改判为未绑定"),
            pytest.param("pending", "remote_onebot", "", InstanceStatus.UNBOUND, id="pending 同样改判"),
            pytest.param("error", "remote_onebot", "", InstanceStatus.UNBOUND, id="没有账号谈不上运行异常"),
            # 绑了账号的实例：offline 就是真的掉了，一个字都不改
            pytest.param("offline", "builtin_napcat", "10001", InstanceStatus.OFFLINE, id="绑了账号才是真掉线"),
            pytest.param("online", "onebot_v11", "10001", InstanceStatus.ONLINE, id="在线不受影响"),
            # 管理员的意图优先于连接事实
            pytest.param("disabled", "remote_onebot", "", InstanceStatus.DISABLED, id="停用意图优先"),
            pytest.param("archived", "remote_onebot", "", InstanceStatus.ARCHIVED, id="归档意图优先"),
            # 更精确的诊断不能被 unbound 盖掉
            pytest.param(
                "login_required",
                "builtin_napcat",
                "",
                InstanceStatus.LOGIN_REQUIRED,
                id="待扫码比未绑定更精确",
            ),
            pytest.param("binding", "openilink", "", InstanceStatus.OFFLINE, id="适配器私有生命周期态不改写"),
            # 原生适配器整个只有一条连接，账号字段对它本来就恒为空
            pytest.param("offline", "native", "", InstanceStatus.OFFLINE, id="原生适配器行为逐字不变"),
        ],
    )
    def test_resolve_instance_status(
        self,
        raw_status: str,
        provider: str,
        account: str,
        expected: InstanceStatus,
    ) -> None:
        assert resolve_instance_status(raw_status, provider, account) is expected

    async def test_created_without_account_is_unbound(self, db) -> None:
        """新建时没填账号 → 直接落 unbound，而不是先假装离线"""
        instance = await create_instance(
            InstanceCreateRequest(
                instance_key=SHELL,
                display_name="Tsubaki",
                adapter_key=ADAPTER,
                provider=InstanceProvider.BUILTIN_NAPCAT,
                provider_account_id="",
            ),
        )
        assert instance.status == STATUS_UNBOUND
        assert (await describe_instance(instance)).status is InstanceStatus.UNBOUND

    async def test_created_with_account_stays_offline(self, db) -> None:
        instance = await create_instance(
            InstanceCreateRequest(
                instance_key="inst9",
                adapter_key=ADAPTER,
                provider=InstanceProvider.REMOTE_ONEBOT,
                provider_account_id="10009",
            ),
        )
        assert instance.status == "offline"

    async def test_created_native_instance_is_unaffected(self, db) -> None:
        """原生适配器（email / minecraft / sse）连接来自适配器自身配置，不存在"绑账号"这回事"""
        instance = await create_instance(
            InstanceCreateRequest(
                instance_key="mail1",
                adapter_key="email",
                provider=InstanceProvider.NATIVE,
                provider_account_id="",
            ),
        )
        assert instance.status == "offline"

    async def test_binding_and_unbinding_move_the_status(self, db) -> None:
        """绑上账号就该等待连接，解绑就该回到未绑定 —— 两个方向都要落库"""
        instance = await seed_instance(SHELL, account="", status=STATUS_UNBOUND)

        bound = await update_instance(instance, InstanceUpdateRequest(provider_account_id="10001"))
        assert bound.status == "offline"

        unbound = await update_instance(bound, InstanceUpdateRequest(provider_account_id=""))
        assert unbound.status == STATUS_UNBOUND

    async def test_enabling_an_accountless_instance_lands_on_unbound(self, db) -> None:
        """启用一个没绑账号的实例不该把它推进"离线"，那会立刻开始掉线计时"""
        instance = await seed_instance(SHELL, account="", status="disabled", enabled=False)

        enabled = await set_instance_enabled(instance, enabled=True)
        assert enabled.status == STATUS_UNBOUND

    async def test_enabling_a_bound_instance_still_lands_on_offline(self, db) -> None:
        instance = await seed_instance("inst9", account="10009", status="disabled", enabled=False)

        enabled = await set_instance_enabled(instance, enabled=True)
        assert enabled.status == "offline"


class TestLegacyRowsAreNormalized:
    """升级前建的空壳实例挂着 offline，要一次性改判过来"""

    async def test_normalize_rewrites_only_the_shell_rows(self, db) -> None:
        await seed_instance(SHELL, account="", status="offline")
        await seed_instance("bound", account="10001", status="offline")
        await seed_instance("scanning", account="", status="login_required")
        await seed_instance("stopped", account="", status="disabled", enabled=False)
        await seed_instance("mail1", account="", status="offline", provider="native", adapter_key="email")

        assert await normalize_unbound_instance_status() == [SHELL]

        statuses = {row.instance_key: row.status for row in await DBAdapterInstance.all()}
        assert statuses == {
            SHELL: STATUS_UNBOUND,
            "bound": "offline",
            "scanning": "login_required",
            "stopped": "disabled",
            "mail1": "offline",
        }

    async def test_normalize_is_idempotent(self, db) -> None:
        await seed_instance(SHELL, account="", status="offline")

        assert await normalize_unbound_instance_status() == [SHELL]
        assert await normalize_unbound_instance_status() == []

    async def test_normalize_agrees_with_the_read_side(self, db) -> None:
        """落库的判据与界面上已经显示的结论必须一致，否则改判会让界面"跳变"一次"""
        await seed_instance(SHELL, account="", status="offline")
        await seed_instance("bound", account="10001", status="offline")

        before = {row.instance_key: resolve_instance_status(row.status, row.provider, row.provider_account_id) for row in await DBAdapterInstance.all()}
        await normalize_unbound_instance_status()
        after = {row.instance_key: resolve_instance_status(row.status, row.provider, row.provider_account_id) for row in await DBAdapterInstance.all()}

        assert before == after
        assert before[SHELL] is InstanceStatus.UNBOUND


class TestHealthMonitorLeavesUnboundAlone:
    """探活不该对一个从未上线过的实例反复告警"""

    @pytest.fixture
    def notifications(self, monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str]]:
        sent: List[Tuple[str, str]] = []

        async def _fake_dispatch(title: str, body: str, level: str = "info") -> Dict[str, bool]:  # noqa: ARG001
            sent.append((level, title))
            return {"stub": True}

        monkeypatch.setattr(health_module, "dispatch_notification", _fake_dispatch)
        return sent

    def test_unbound_is_an_unmanaged_status(self) -> None:
        assert InstanceStatus.UNBOUND.value in UNMANAGED_STATUSES

    async def test_probe_skips_unbound_and_never_alerts(
        self,
        db,
        notifications: List[Tuple[str, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """改前：`inst3` 每个探活周期被判一次掉线，到点发一条「实例掉线告警」，永远不会自愈"""
        await seed_instance(SHELL, account="", status=STATUS_UNBOUND)
        monkeypatch.setattr(health_module.config, "INSTANCE_OFFLINE_ALERT_DELAY_SECONDS", 0)

        service = InstanceHealthService()
        await service._probe_all()  # noqa: SLF001
        await service._probe_all()  # noqa: SLF001

        assert (await DBAdapterInstance.get(instance_key=SHELL)).status == STATUS_UNBOUND
        assert notifications == []

    async def test_health_snapshot_marks_it_unmonitored(self, db) -> None:
        """界面要能看出「它不在监测范围内」，而不是一个安静的红点"""
        await seed_instance(SHELL, account="", status=STATUS_UNBOUND)

        snapshot = {item.instance_key: item for item in (await InstanceHealthService().list_health()).items}
        assert snapshot[SHELL].monitored is False


class TestUnboundIsReportedWhereverOfflineIs:
    """把一档从 offline 里拆出来，就必须在**每一个**播报 offline 的地方补上它

    否则那批实例既不在线、也不离线、也不待认领 —— 数字加不回总数，而它们恰恰是
    「我建的实例一直不收消息」最先该被看见的一批。运维播报与总后台首页是同一份
    `ConsoleOverview`，两边都得说。
    """

    @pytest.fixture(autouse=True)
    def no_docker(self, monkeypatch: pytest.MonkeyPatch):
        """总览会顺手探一次宿主机 Docker，用例不依赖宿主机上有没有 Docker"""
        from nekro_agent.services.console import instance_manager as manager_module

        async def _unavailable() -> bool:
            return False

        monkeypatch.setattr(manager_module, "probe_docker_available", _unavailable)

    async def test_ops_report_names_the_unbound_bucket(self, db) -> None:
        from nekro_agent.services.ops_bot.reports import render_overview

        await seed_field_layout()  # AUTO=unclaimed, SHELL=未绑定账号
        await seed_instance("obbound", account="10001", status="offline")
        await seed_instance("oblive", account="10002", status="online")

        text = await render_overview()

        assert "未绑定账号: 1" in text
        assert "离线: 1" in text
        assert "待认领: 1" in text
        assert "实例总数: 4" in text

    async def test_console_overview_buckets_add_up(self, db) -> None:
        """四档相加等于总数：拆出 unbound 之后没有任何一行掉进缝里"""
        from nekro_agent.services.console.instance_manager import build_overview

        await seed_field_layout()
        await seed_instance("obbound", account="10001", status="offline")
        await seed_instance("oblive", account="10002", status="online")

        overview = await build_overview()

        assert (
            overview.instance_online
            + overview.instance_offline
            + overview.instance_unbound
            + overview.instance_unclaimed
        ) == overview.instance_total


# ---------------------------------------------------------------------------
# 2. 待认领清单
# ---------------------------------------------------------------------------


class TestUnclaimedListing:
    """「有 N 个协议端已连接但未认领」这件事要能被数出来"""

    async def test_lists_unclaimed_with_their_channel_counts(self, db) -> None:
        """会话数是这份清单最要紧的一列：它回答「我在频道管理里找不到的会话在哪」"""
        await seed_field_layout()

        ends = await list_unclaimed_protocol_ends()

        assert [end.instance.instance_key for end in ends] == [AUTO]
        assert ends[0].instance.provider_account_id == ACCOUNT
        assert ends[0].stats.channel_count == 2

    async def test_adopt_targets_exclude_unclaimed_and_archived(self, db) -> None:
        await seed_field_layout()
        await seed_instance("archived_one", account="", status="archived", enabled=False)
        await seed_instance("dead", account="", status="deleted", enabled=False)
        await seed_instance("wx1", account="", status="offline", adapter_key="wechat_ilink_multi")

        targets = await list_adopt_targets({ADAPTER})

        assert [target.instance_key for target in targets] == [SHELL]

    async def test_adopt_targets_exclude_disabled_instances(self, db) -> None:
        """清单里不出现停用实例，与 `adopt_unclaimed_instance` 的拦截同一判据

        两处判据必须一致：只在接口上拦、清单里照旧列出来，管理员会一路点到最后一步才被拒；
        只在清单里滤、接口上放行，则任何直接调用（脚本、旧前端）都能把 Bot 弄哑。
        """
        await seed_field_layout()
        await seed_instance("off_by_status", account="", status="disabled", enabled=False)
        await seed_instance("off_by_flag", account="", status="offline", enabled=False)

        targets = await list_adopt_targets({ADAPTER})

        assert [target.instance_key for target in targets] == [SHELL]

    async def test_endpoint_reports_both_sides_in_one_response(self, db) -> None:
        await seed_field_layout()

        response = await get_console_unclaimed_instances(_current_user=platform_owner())

        assert response.total == 1
        assert response.items[0].instance_key == AUTO
        assert response.items[0].channel_count == 2
        assert [target.instance_key for target in response.adopt_targets] == [SHELL]
        # 目标实例的状态也要说清楚它为什么"离线"
        assert response.adopt_targets[0].status is InstanceStatus.UNBOUND

    async def test_tenant_is_denied_by_capability_not_by_existence(self, db) -> None:
        """认领是平台方能力，租户拿 403（能力边界），而不是 404（资源存在性）"""
        await seed_field_layout()

        with pytest.raises(HTTPException) as excinfo:
            await get_console_unclaimed_instances(_current_user=acme_admin())
        assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# 3. 认领为新实例
# ---------------------------------------------------------------------------


class TestClaimAsNewInstance:
    """把 `ob{QQ号}` 认领成一个管理员自己命名的实例"""

    async def test_claim_in_place_when_no_new_key_given(self, db) -> None:
        """留空新标识 = 就地认领，一行数据都不动（改造前的行为逐字保留）"""
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)

        outcome = await claim_unclaimed_as_new_instance(source, "Tsubaki", "")

        assert outcome.target.instance_key == AUTO
        assert outcome.target.display_name == "Tsubaki"
        assert outcome.migration is None
        assert outcome.source_archived is False
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 2

    async def test_claim_with_new_key_moves_everything(self, db) -> None:
        """认领为新标识：账号、频道、消息、chat_key 全部跟到新实例，源实例归档"""
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)

        outcome = await claim_unclaimed_as_new_instance(source, "Tsubaki", WANTED)

        assert outcome.target.instance_key == WANTED
        assert outcome.target.display_name == "Tsubaki"
        assert outcome.target.provider_account_id == ACCOUNT
        assert outcome.account_moved is True
        assert outcome.source_archived is True

        # 历史真的跟过去了
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 0
        assert await DBChatChannel.filter(instance_key=WANTED).count() == 2
        assert set(await DBChatChannel.filter(instance_key=WANTED).values_list("chat_key", flat=True)) == {
            f"{ADAPTER}-{WANTED}-{GROUP}",
            f"{ADAPTER}-{WANTED}-{OTHER_GROUP}",
        }
        assert await DBChatMessage.filter(chat_key=f"{ADAPTER}-{WANTED}-{GROUP}").count() == 2
        assert await DBChatMessage.filter(instance_key=AUTO).count() == 0

        # 源实例还在，但已经不再占着账号，也不再是"待认领"
        archived = await DBAdapterInstance.get(instance_key=AUTO)
        assert archived.provider_account_id == ""
        assert archived.status == "archived"
        assert archived.enabled is False

    async def test_new_instance_inherits_tenant_ownership(self, db) -> None:
        """未认领实例若已被划归某租户，新建的那一行必须跟着走，否则认领会把它交回平台方"""
        await seed_instance(AUTO, account=ACCOUNT, status="unclaimed", tenant_id=ACME_TENANT_ID)
        source = await DBAdapterInstance.get(instance_key=AUTO)

        outcome = await claim_unclaimed_as_new_instance(source, "客服机器人", WANTED)

        assert outcome.target.tenant_id == ACME_TENANT_ID

    @pytest.mark.parametrize("bad_key", ["Tsubaki-1", "a" * 33, "有中文"])
    async def test_invalid_new_key_is_rejected(self, db, bad_key: str) -> None:
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)

        with pytest.raises(ValidationError):
            await claim_unclaimed_as_new_instance(source, "Tsubaki", bad_key)
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 2

    async def test_taken_new_key_is_a_conflict(self, db) -> None:
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)

        with pytest.raises(ConflictError):
            await claim_unclaimed_as_new_instance(source, "Tsubaki", SHELL)

    async def test_account_conflict_does_not_leave_an_orphan_instance(self, db) -> None:
        """账号被第三行占着时，认领必须在**建行之前**就失败

        否则新实例已经落库、认领却抛异常，管理员的列表里会多出一个谁都不认识的空实例，
        而他并不知道那是上一次失败的残骸。
        """
        await seed_field_layout()
        # 一个残留的重复绑定：同一个账号还挂在另一行上
        await seed_instance("stale", account=ACCOUNT, status="offline")
        source = await DBAdapterInstance.get(instance_key=AUTO)

        with pytest.raises(ConflictError):
            await claim_unclaimed_as_new_instance(source, "Tsubaki", WANTED)

        assert await DBAdapterInstance.filter(instance_key=WANTED).count() == 0
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 2

    async def test_already_claimed_instance_cannot_be_claimed_again(self, db) -> None:
        """幂等：第二次认领同一个协议端要被明确拒绝，而不是再搬一次"""
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)
        await claim_unclaimed_as_new_instance(source, "Tsubaki", WANTED)

        await source.refresh_from_db()
        with pytest.raises(ValidationError):
            await claim_unclaimed_as_new_instance(source, "Tsubaki", "another")


# ---------------------------------------------------------------------------
# 4. 并入已有实例
# ---------------------------------------------------------------------------


class TestAdoptIntoExistingInstance:
    """把协议端并进管理员早就建好的那个实例（现场里就是 Tsubaki）"""

    async def test_history_follows_and_account_moves(self, db) -> None:
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        outcome = await adopt_unclaimed_instance(source, target)

        assert outcome.account_moved is True
        assert outcome.source_archived is True
        assert outcome.migration is not None
        assert sorted(outcome.migration.moved_channels) == sorted([GROUP, OTHER_GROUP])

        target = await DBAdapterInstance.get(instance_key=SHELL)
        assert target.provider_account_id == ACCOUNT
        # 连接此刻还挂在协议端上，真实状态交给掉线监测下一轮刷新
        assert target.status == "connecting"

        assert await DBChatChannel.filter(instance_key=SHELL).count() == 2
        assert await DBChatMessage.filter(chat_key=f"{ADAPTER}-{SHELL}-{GROUP}").count() == 2
        assert (await DBAdapterInstance.get(instance_key=AUTO)).provider_account_id == ""

    async def test_conflicting_channel_is_merged_not_duplicated(self, db) -> None:
        """目标实例名下已经有同一个群时按合并处理，复用归属迁移里既有的合并规则"""
        await seed_field_layout()
        await seed_channel(SHELL, GROUP, channel_name="目标实例这边的同一个群")
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        outcome = await adopt_unclaimed_instance(source, target)

        assert outcome.migration is not None
        assert outcome.migration.merged_channels == [GROUP]
        assert outcome.migration.moved_channels == [OTHER_GROUP]
        assert await DBChatChannel.filter(instance_key=SHELL, channel_id=GROUP).count() == 1
        assert await DBChatMessage.filter(chat_key=f"{ADAPTER}-{SHELL}-{GROUP}").count() == 2

    @pytest.mark.parametrize(
        ("enabled", "status"),
        [
            (False, "disabled"),
            # 只改了一列的脏行：`enabled=False` 但状态还是运行态，收发闸门照样是关的
            (False, "offline"),
            (True, "disabled"),
        ],
    )
    async def test_disabled_target_is_rejected(self, db, enabled: bool, status: str) -> None:
        """停用的实例不能接管一个正在收消息的协议端

        停用意味着收发闸门关着（`DBAdapterInstance.is_instance_active`）。放行的话这一次认领会
        把账号过户给一个不派发消息的实例、把历史一并搬走、再把源实例归档 —— 这条 Bot 当场彻底
        静默，而界面上没有任何一处会说它坏了。本流程存在的理由就是「我的实例收不到消息」，
        不能自己再造一个同样的现场；正确的顺序是先启用目标实例再认领。
        """
        await seed_field_layout()
        await DBAdapterInstance.filter(instance_key=SHELL).update(enabled=enabled, status=status)
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        with pytest.raises(ValidationError):
            await adopt_unclaimed_instance(source, target)

        # 一行都不许动：账号仍在协议端那边，历史仍在源实例名下，源实例仍是未认领
        source = await DBAdapterInstance.get(instance_key=AUTO)
        assert source.provider_account_id == ACCOUNT
        assert source.status == "unclaimed"
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 2
        assert await DBChatChannel.filter(instance_key=SHELL).count() == 0
        target = await DBAdapterInstance.get(instance_key=SHELL)
        assert target.provider_account_id == ""

    async def test_target_already_holding_another_account_is_rejected(self, db) -> None:
        """一个实例一个账号：目标已经有自己的号时必须先解绑，不能悄悄顶替"""
        await seed_field_layout()
        await DBAdapterInstance.filter(instance_key=SHELL).update(provider_account_id="10001")
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        with pytest.raises(ConflictError):
            await adopt_unclaimed_instance(source, target)
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 2

    async def test_cross_adapter_is_rejected(self, db) -> None:
        await seed_field_layout()
        wx = await seed_instance("wx1", account="", status="offline", adapter_key="wechat_ilink_multi")
        source = await DBAdapterInstance.get(instance_key=AUTO)

        with pytest.raises(ValidationError):
            await adopt_unclaimed_instance(source, wx)

    async def test_archived_target_is_rejected(self, db) -> None:
        await seed_field_layout()
        await DBAdapterInstance.filter(instance_key=SHELL).update(status="archived", enabled=False)
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        with pytest.raises(ValidationError):
            await adopt_unclaimed_instance(source, target)

    async def test_partial_failure_keeps_the_account_on_the_source(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """一个频道搬失败 → 整体停在第一步，界面上仍是「有一个未认领的协议端」，重跑可补齐

        反过来（账号已过户、历史却少了一半）没有任何入口能修回来。
        """
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        from nekro_agent.services import chat_key_migration as migration_module

        original = migration_module._migrate_one_channel  # noqa: SLF001

        async def flaky(adapter_key: str, item, target_instance_key: str):
            if item.channel_id == OTHER_GROUP:
                raise RuntimeError("模拟一次数据库抖动")
            return await original(adapter_key, item, target_instance_key)

        monkeypatch.setattr(migration_module, "_migrate_one_channel", flaky)

        outcome = await adopt_unclaimed_instance(source, target)

        assert outcome.account_moved is False
        assert outcome.source_archived is False
        assert outcome.migration is not None
        assert outcome.migration.failed_channels == [OTHER_GROUP]

        source = await DBAdapterInstance.get(instance_key=AUTO)
        assert source.provider_account_id == ACCOUNT
        assert source.status == "unclaimed"
        # 已经搬完的那个频道不回退
        assert await DBChatChannel.filter(instance_key=SHELL, channel_id=GROUP).count() == 1

    async def test_retry_after_partial_failure_completes(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """半成品必须是可重试的：第二次跑（不再抖动）把剩下的补齐并完成过户"""
        await seed_field_layout()
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)

        from nekro_agent.services import chat_key_migration as migration_module

        original = migration_module._migrate_one_channel  # noqa: SLF001
        failures = {OTHER_GROUP}

        async def flaky(adapter_key: str, item, target_instance_key: str):
            if item.channel_id in failures:
                failures.discard(item.channel_id)
                raise RuntimeError("模拟一次数据库抖动")
            return await original(adapter_key, item, target_instance_key)

        monkeypatch.setattr(migration_module, "_migrate_one_channel", flaky)

        await adopt_unclaimed_instance(source, target)
        source = await DBAdapterInstance.get(instance_key=AUTO)
        target = await DBAdapterInstance.get(instance_key=SHELL)
        outcome = await adopt_unclaimed_instance(source, target)

        assert outcome.migration is not None
        assert outcome.migration.failed_channels == []
        assert outcome.account_moved is True
        assert await DBChatChannel.filter(instance_key=SHELL).count() == 2
        assert (await DBAdapterInstance.get(instance_key=SHELL)).provider_account_id == ACCOUNT


# ---------------------------------------------------------------------------
# 5. 路由层：权限与二次确认
# ---------------------------------------------------------------------------


class TestClaimEndpoints:
    """端点层的守卫、确认串与结果契约"""

    async def test_claim_endpoint_reports_the_migration(self, db) -> None:
        await seed_field_layout()

        result = await claim_console_instance(
            AUTO,
            InstanceClaimRequest(display_name="Tsubaki", instance_key=WANTED),
            _current_user=platform_owner(),
        )

        assert result.ok is True
        assert result.instance.instance_key == WANTED
        assert result.instance.provider_account_id == ACCOUNT
        assert result.source_instance_key == AUTO
        assert result.account_moved is True
        assert result.migration is not None
        assert sorted(result.migration.moved_channels) == sorted([GROUP, OTHER_GROUP])

    async def test_adopt_requires_the_confirmation_string(self, db) -> None:
        """二次确认与彻底删除、历史归属迁移同一套形态"""
        await seed_field_layout()

        with pytest.raises(ValidationError):
            await adopt_console_instance(
                SHELL,
                InstanceAdoptRequest(source_instance_key=AUTO, confirm_source_instance_key="typo"),
                _current_user=platform_owner(),
            )
        assert await DBChatChannel.filter(instance_key=AUTO).count() == 2

    async def test_adopt_endpoint_moves_everything(self, db) -> None:
        await seed_field_layout()

        result = await adopt_console_instance(
            SHELL,
            InstanceAdoptRequest(source_instance_key=AUTO, confirm_source_instance_key=AUTO),
            _current_user=platform_owner(),
        )

        assert result.ok is True
        assert result.instance.instance_key == SHELL
        assert result.instance.provider_account_id == ACCOUNT
        assert result.source_archived is True
        assert await DBChatChannel.filter(instance_key=SHELL).count() == 2

    async def test_tenant_cannot_reach_the_claim_endpoints(self, db) -> None:
        """认领两条出路都是 `Role.Super`，租户恒 403（能力边界，不泄露实例是否存在）"""
        await seed_field_layout()

        for call in (
            claim_console_instance(AUTO, InstanceClaimRequest(display_name="x"), _current_user=acme_admin()),
            adopt_console_instance(
                SHELL,
                InstanceAdoptRequest(source_instance_key=AUTO, confirm_source_instance_key=AUTO),
                _current_user=acme_admin(),
            ),
        ):
            with pytest.raises(HTTPException) as excinfo:
                await call
            assert excinfo.value.status_code == 403

    async def test_unknown_instance_is_404(self, db) -> None:
        await seed_field_layout()

        with pytest.raises(NotFoundError):
            await claim_console_instance(
                "nope",
                InstanceClaimRequest(display_name="x"),
                _current_user=platform_owner(),
            )
