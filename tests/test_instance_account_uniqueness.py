"""一个协议端账号只能绑一个实例的回归测试

现场形态（PROJECT_STATE §3 #31）：协议端先自行连上被登记成 `ob{账号}`，管理员随后在总后台
新建实例又填了同一个账号。两行绑同一账号之后，入站链路第一步 `resolve_instance_key` 的
`get_or_none` 直接抛 `MultipleObjectsReturned`，该账号的消息全部处理不了。

唯一性目前**只在代码层**把关（数据库约束为什么加不上，见 `models/db_adapter_instance.py`
的模块文档），所以这三层缺一不可，本文件逐层钉住：

**写侧不再产生重复**
- 创建/更新时账号已被占用就拒绝，且未认领实例照样算占用（它正是最常见的占用方）
- 认领流程不受影响：认领改的是既有那一行，不新增行
- 未绑定账号（空串）不是占用，多个实例可以同时处于未绑定状态

**存量与残余竞争窗口能被收拾**
- 保留最早那行、其余解绑，实例本身与其数据一行不删，且留下可追溯的痕迹
- 保留的正是读侧会选中的那一行——修复不改变消息此刻实际投递到的实例

**读侧在脏数据面前不崩**
- 库里真有重复时确定性地取最早那行并告警，而不是打掉整条入站链路
- 「确定性」必须挺得住出站方向：出站解析只能写 `实例 -> 账号`，写反向会把入站悄悄改判

**软删墓碑不算占用**
- 墓碑不在总后台列表里，让它占着账号 = 管理员被一个界面上不存在的实例挡住
- 存量修复更不能按"最早那行"把账号判给墓碑、反手解绑正在收发消息的那个实例

**「墓碑不算占用」的另一半：复活也是一条账号写入路径**
墓碑躺着的那段时间里账号可以被别人合法绑走，于是「把一行从 `deleted` 变回活的」会重新
写出两行绑同一账号的活实例；而 `get_by_bound_account` 取最早那行、墓碑的 id 通常更小，
入站消息会从一直在收发的那个实例**静默改判**给刚复活的空壳。

**自动登记那一支同样不能崩、也不能把消息交给墓碑**
写侧校验只管得住"人来写"的路径，协议端自行连入是第四条：按标识取行的这一步不排除墓碑
（占用判定排除），一行叫 `ob{QQ}` 的墓碑会被当成"就是它"；而退让标识 `ob{QQ}x` 改造前是
直接 `create`，它一旦也被占着就是一条 `IntegrityError` 从入站链路第一步冲出去。
"""

from typing import List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import instance as instance_module
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.schemas.console import (
    InstanceCreateRequest,
    InstanceProvider,
    InstanceUpdateRequest,
)
from nekro_agent.schemas.errors import ConflictError
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.console.instance_manager import (
    METADATA_UNBOUND_ACCOUNT_CONFLICT,
    claim_instance,
    create_instance,
    load_metadata,
    set_instance_enabled,
    unbind_duplicate_account_bindings,
    update_instance,
)

ADAPTER = "onebot_v11"
OTHER_ADAPTER = "wechat_ilink_multi"
ACCOUNT = "10001"
OTHER_ACCOUNT = "10002"
# 协议端自行连入时由 `resolve_instance_key` 生成的标识
AUTO_KEY = "ob10001"
# 管理员事后在总后台新建的实例
ADMIN_KEY = "inst1"


async def seed_instance(
    *,
    instance_key: str,
    provider_account_id: str = "",
    adapter_key: str = ADAPTER,
    status: str = "offline",
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        provider="onebot_v11",
        provider_account_id=provider_account_id,
        status=status,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    instance_module.clear_instance_cache()
    try:
        yield
    finally:
        instance_module.clear_instance_cache()
        await Tortoise.close_connections()


def create_body(instance_key: str, provider_account_id: str, adapter_key: str = ADAPTER) -> InstanceCreateRequest:
    return InstanceCreateRequest(
        instance_key=instance_key,
        display_name=instance_key,
        adapter_key=adapter_key,
        provider=InstanceProvider.REMOTE_ONEBOT,
        provider_account_id=provider_account_id,
    )


class TestCreateAndUpdateRejectTakenAccount:
    """创建与更新侧的账号唯一性校验"""

    async def test_create_rejects_account_held_by_unclaimed_instance(self, db) -> None:
        """现场形态：协议端已自动登记成未认领实例，管理员再建一个绑同账号的实例

        未认领实例必须算占用——它正是这条路径最常见的占用方。报错要指出该去认领谁，
        否则管理员只知道"建不了"，不知道那个账号的实例其实已经在列表里等着被认领。
        """
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="unclaimed")

        with pytest.raises(ConflictError) as excinfo:
            await create_instance(create_body(ADMIN_KEY, ACCOUNT))

        message = str(excinfo.value)
        assert ACCOUNT in message
        assert AUTO_KEY in message
        assert "认领" in message
        assert await DBAdapterInstance.filter(instance_key=ADMIN_KEY).count() == 0

    async def test_create_rejects_account_held_by_archived_instance(self, db) -> None:
        """已归档实例仍然占着账号：读侧 `get_by_bound_account` 同样不看状态，
        放行它等于把入站路由的歧义原样放回来。提示语因此改为「先解绑」而不是「去认领」。
        """
        await seed_instance(instance_key="old", provider_account_id=ACCOUNT, status="archived")

        with pytest.raises(ConflictError) as excinfo:
            await create_instance(create_body(ADMIN_KEY, ACCOUNT))
        assert "解绑" in str(excinfo.value)

    async def test_create_allows_same_account_under_other_adapter(self, db) -> None:
        """账号号码是按适配器命名空间的，QQ 的 10001 与另一个平台的 10001 毫无关系"""
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT)

        created = await create_instance(create_body("wx1", ACCOUNT, adapter_key=OTHER_ADAPTER))
        assert created.provider_account_id == ACCOUNT
        assert created.adapter_key == OTHER_ADAPTER

    async def test_create_allows_multiple_unbound_instances(self, db) -> None:
        """「实例已建好、还没扫码登录」是常态，空账号绝不能被判成互相冲突"""
        first = await create_instance(create_body("insta", ""))
        second = await create_instance(create_body("instb", ""))
        assert (first.provider_account_id, second.provider_account_id) == ("", "")

    async def test_update_rejects_rebinding_to_taken_account(self, db) -> None:
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT)
        target = await seed_instance(instance_key=ADMIN_KEY, provider_account_id=OTHER_ACCOUNT)

        with pytest.raises(ConflictError):
            await update_instance(target, InstanceUpdateRequest(provider_account_id=ACCOUNT))

        await target.refresh_from_db()
        assert target.provider_account_id == OTHER_ACCOUNT

    async def test_update_keeps_own_account_without_false_conflict(self, db) -> None:
        """提交与当前值相同的账号不是冲突——界面整表回填时这是最常见的一次提交"""
        target = await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT)

        updated = await update_instance(
            target,
            InstanceUpdateRequest(display_name="renamed", provider_account_id=ACCOUNT),
        )
        assert updated.provider_account_id == ACCOUNT
        assert updated.display_name == "renamed"

    async def test_update_can_unbind_and_hand_the_account_over(self, db) -> None:
        """解绑是把账号腾给另一个实例的正规手段，必须始终可用"""
        target = await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT)

        updated = await update_instance(target, InstanceUpdateRequest(provider_account_id=""))
        assert updated.provider_account_id == ""

        rebound = await seed_instance(instance_key="inst2", provider_account_id="")
        rebound = await update_instance(rebound, InstanceUpdateRequest(provider_account_id=ACCOUNT))
        assert rebound.provider_account_id == ACCOUNT

    async def test_claim_flow_is_not_blocked(self, db) -> None:
        """认领改的是既有那一行的状态，不新增行，因此不会撞上账号唯一性"""
        unclaimed = await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="unclaimed")

        claimed = await claim_instance(unclaimed, "客服机器人")
        assert claimed.display_name == "客服机器人"
        assert claimed.provider_account_id == ACCOUNT
        assert await DBAdapterInstance.filter(provider_account_id=ACCOUNT).count() == 1


class TestTombstoneDoesNotHoldAccounts:
    """软删墓碑（`soft_delete_instance` 留下的 `deleted` 行）不算占用

    墓碑不清空账号字段，却被 `list_instances` 过滤掉。让它继续占着账号，管理员会被一个
    界面上根本不存在的实例挡住，而文案还在让他去给那个实例解绑——一条走不出去的路。
    这条路径今天就走得通：`adapters/wechat_ilink_multi/routers.py` 的删除接口留的正是墓碑。
    """

    async def test_tombstone_does_not_block_rebinding(self, db) -> None:
        await seed_instance(instance_key="dead", provider_account_id=ACCOUNT, status="deleted")

        created = await create_instance(create_body(ADMIN_KEY, ACCOUNT))
        assert created.provider_account_id == ACCOUNT

    async def test_tombstone_is_never_the_inbound_target(self, db) -> None:
        """墓碑是已删除的实例，读侧不能把入站消息投给它"""
        await seed_instance(instance_key="dead", provider_account_id=ACCOUNT, status="deleted")
        await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT)

        picked = await DBAdapterInstance.get_by_bound_account(ADAPTER, ACCOUNT)
        assert picked is not None
        assert picked.instance_key == ADMIN_KEY

    async def test_repair_never_unbinds_a_live_instance_in_favour_of_a_tombstone(self, db) -> None:
        """墓碑的 id 通常比接手的新实例小，按"最早那行"会把正在收发消息的那个解绑掉"""
        await seed_instance(instance_key="dead", provider_account_id=ACCOUNT, status="deleted")
        live = await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT, status="online")

        assert await unbind_duplicate_account_bindings() == []

        await live.refresh_from_db()
        assert live.provider_account_id == ACCOUNT
        assert live.last_error == ""


class TestDuplicateBindingRepair:
    """存量库、以及创建与协议端自行连入并发时残留的重复绑定"""

    @staticmethod
    async def _seed_conflict() -> None:
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="unclaimed")
        await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT)
        await seed_instance(instance_key="lonely", provider_account_id=OTHER_ACCOUNT)
        await seed_instance(instance_key="unbound", provider_account_id="")

    async def test_repair_keeps_earliest_and_unbinds_the_rest(self, db) -> None:
        """保留最早那行（绑定该账号最久、也就是历史数据的归属方），其余只解绑不删

        两个实例按隔离模型是两个合法的实例，各自可能已有会话与记忆，替用户决定谁并进谁
        就是替他丢数据——那件事有专门的、要先看预览的归属迁移入口。
        """
        await self._seed_conflict()

        repairs = await unbind_duplicate_account_bindings()

        assert len(repairs) == 1
        assert repairs[0].provider_account_id == ACCOUNT
        assert repairs[0].kept_instance_key == AUTO_KEY
        assert repairs[0].unbound_instance_keys == [ADMIN_KEY]

        # 实例本身一行不删
        assert await DBAdapterInstance.all().count() == 4
        keeper = await DBAdapterInstance.get(instance_key=AUTO_KEY)
        loser = await DBAdapterInstance.get(instance_key=ADMIN_KEY)
        assert keeper.provider_account_id == ACCOUNT
        assert loser.provider_account_id == ""

        marker = load_metadata(loser)[METADATA_UNBOUND_ACCOUNT_CONFLICT]
        assert marker["provider_account_id"] == ACCOUNT
        assert marker["kept_by"] == AUTO_KEY
        assert ACCOUNT in loser.last_error

    async def test_repair_keeps_the_row_the_read_side_would_have_picked(self, db) -> None:
        """修复不能改变消息此刻实际投递到的实例，只能把歧义消掉"""
        await self._seed_conflict()

        picked_before = await DBAdapterInstance.get_by_bound_account(ADAPTER, ACCOUNT)
        repairs = await unbind_duplicate_account_bindings()
        picked_after = await DBAdapterInstance.get_by_bound_account(ADAPTER, ACCOUNT)

        assert picked_before is not None
        assert picked_after is not None
        assert picked_before.instance_key == picked_after.instance_key == repairs[0].kept_instance_key

    async def test_repair_leaves_healthy_rows_alone_and_is_idempotent(self, db) -> None:
        await self._seed_conflict()

        await unbind_duplicate_account_bindings()
        assert await unbind_duplicate_account_bindings() == []

        lonely = await DBAdapterInstance.get(instance_key="lonely")
        unbound = await DBAdapterInstance.get(instance_key="unbound")
        assert lonely.provider_account_id == OTHER_ACCOUNT
        assert load_metadata(lonely) == {}
        assert unbound.provider_account_id == ""
        assert load_metadata(unbound) == {}

    async def test_repair_frees_the_account_for_a_new_binding(self, db) -> None:
        """修完之后每个账号只剩一行绑定，管理员随后的改绑不会再被"占用"挡住"""
        await self._seed_conflict()
        await unbind_duplicate_account_bindings()

        assert await DBAdapterInstance.filter(provider_account_id=ACCOUNT).count() == 1
        loser = await DBAdapterInstance.get(instance_key=ADMIN_KEY)
        rebound = await update_instance(loser, InstanceUpdateRequest(provider_account_id="10003"))
        assert rebound.provider_account_id == "10003"


class TestInboundResolutionTolerance:
    """读侧：库里真有重复也不能打掉整条入站链路"""

    async def test_duplicate_bindings_resolve_deterministically(self, db) -> None:
        """改前这里是 `get_or_none`，三次全部抛 `MultipleObjectsReturned`（服务器上 3/3 实测）"""
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="unclaimed")
        await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT)

        resolved: List[str] = []
        for _ in range(3):
            instance_module.clear_instance_cache()
            resolved.append(await instance_module.resolve_instance_key(ACCOUNT))

        assert resolved == [AUTO_KEY, AUTO_KEY, AUTO_KEY]

    async def test_unregistered_account_is_still_auto_claimed(self, db) -> None:
        """自动登记是刻意的：直接拒绝会让管理员在总后台看不到任何线索，不知道该认领谁"""
        resolved = await instance_module.resolve_instance_key("10009")

        registered = await DBAdapterInstance.get(instance_key=resolved)
        assert registered.provider_account_id == "10009"
        assert registered.status == "unclaimed"

    async def test_empty_account_never_matches_unbound_instances(self, db) -> None:
        """空号码不能当查询条件：它会匹配到全部未绑定实例，把消息静默投给毫不相干的实例"""
        await seed_instance(instance_key="insta", provider_account_id="")
        await seed_instance(instance_key="instb", provider_account_id="")

        with pytest.raises(ValueError, match="账号号码"):
            await instance_module.resolve_instance_key("")

    async def test_outbound_resolution_does_not_repoint_inbound_routing(self, db) -> None:
        """出站方向不得改写 `账号 -> 实例`

        重复绑定还没被收拾干净时（创建/改绑与协议端自行连入并发留下的窗口），
        出站解析曾经顺手把反向缓存也写上，于是任意一次主动推送 / 定时任务 / 掉线巡检
        就把该账号的入站消息整体改判给本次出站的那个实例——静默，且没有任何告警。
        """
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="unclaimed")
        await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT)

        # 出站先发生：从后来那一行解析账号，缓存被预热
        assert await instance_module.resolve_account_id(ADMIN_KEY) == ACCOUNT
        assert instance_module.lookup_cached_account_id(ADMIN_KEY) == ACCOUNT

        # 入站仍须落在读侧判据选中的那一行上
        assert await instance_module.resolve_instance_key(ACCOUNT) == AUTO_KEY

    @pytest.mark.parametrize("account", ["", "10404"])
    async def test_get_by_bound_account_returns_none_for_unbound_lookups(self, db, account: str) -> None:
        result: Optional[DBAdapterInstance] = await DBAdapterInstance.get_by_bound_account(ADAPTER, account)
        assert result is None


async def live_account_holders(account: str = ACCOUNT) -> List[str]:
    """当下真正占着该账号的实例标识（判据与 `find_account_binding_conflict` 逐字对应）"""
    return [
        row.instance_key
        for row in await DBAdapterInstance.filter(adapter_key=ADAPTER, provider_account_id=account)
        .exclude(status="deleted")
        .order_by("id")
    ]


class TestRevivingATombstoneIsAnAccountWrite:
    """把一行从 `deleted` 变回活的 = 一次账号写入，必须过同一道占用校验

    墓碑不算占用是刻意的（见上一组），代价是它躺着的那段时间里账号会被别人合法绑走。
    复活不做校验就等于把 #31 的现场从另一扇门放回来，而且这一次没有任何人在写账号列。
    """

    @staticmethod
    async def _tombstone_whose_account_was_taken() -> DBAdapterInstance:
        tomb = await seed_instance(instance_key="dead", provider_account_id=ACCOUNT, status="deleted")
        await create_instance(create_body(ADMIN_KEY, ACCOUNT))
        return tomb

    async def test_enabling_a_tombstone_releases_an_account_someone_else_now_holds(self, db) -> None:
        tomb = await self._tombstone_whose_account_was_taken()

        revived = await set_instance_enabled(tomb, enabled=True)

        assert await live_account_holders() == [ADMIN_KEY]
        await revived.refresh_from_db()
        assert revived.provider_account_id == ""
        # 「我启用的实例怎么没了账号」必须在总后台看得见，否则这是一次静默改写
        assert ACCOUNT in revived.last_error
        assert ADMIN_KEY in revived.last_error

    async def test_revived_instance_lands_on_unbound_not_offline(self, db) -> None:
        """解绑要发生在算「空闲状态」之前

        顺序反了会落成 `offline`：掉线监测随即为一个不可能有连接的实例计时并告警，
        而那正是 `unbound` 这个状态当初被分出来要消灭的误导。
        """
        tomb = await self._tombstone_whose_account_was_taken()

        revived = await set_instance_enabled(tomb, enabled=True)

        assert revived.status == "unbound"

    async def test_enabling_a_tombstone_keeps_an_account_that_is_still_free(self, db) -> None:
        """没人抢走账号时复活分毫不变：账号留着、状态回到 offline"""
        tomb = await seed_instance(instance_key="dead", provider_account_id=ACCOUNT, status="deleted")

        revived = await set_instance_enabled(tomb, enabled=True)

        assert revived.provider_account_id == ACCOUNT
        assert revived.status == "offline"
        assert revived.last_error == ""
        assert await live_account_holders() == ["dead"]

    async def test_set_status_itself_guards_every_other_caller(self, db) -> None:
        """守卫钉在 `set_status` 上，而不是只钉在总后台那一个调用点

        适配器自己的状态流转（`wechat_ilink_multi` 的绑定/续期链路）同样会把一行带出墓碑态，
        只覆盖总后台的话，这里就是一条谁都没看的第二实现。
        """
        tomb = await self._tombstone_whose_account_was_taken()

        revived = await adapter_instance_service.set_status(tomb, "offline", message="适配器自行恢复")

        assert revived.provider_account_id == ""
        assert await live_account_holders() == [ADMIN_KEY]
        # 落到 offline 时 `last_error` 本会被清空，解绑说明必须挤掉那个空串
        assert ACCOUNT in revived.last_error
        assert ADMIN_KEY in revived.last_error

    async def test_status_changes_outside_the_tombstone_never_touch_the_account(self, db) -> None:
        """普通状态流转（每条消息、每轮探活都会走）一行账号都不该动

        **对照组必须能红。** 第一版这里种的第二行是墓碑，而墓碑本来就被
        `find_account_binding_conflict` 排除——把守卫改成"每次状态写入都核对"这条过度施用的变异
        照样全绿，等于这条断言在任何实现下都成立。现在种的是一行**活着的**冲突实例
        （#31 的脏数据现场），守卫一旦放宽到墓碑以外，这一行的账号会被当场解掉。
        """
        live = await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT, status="offline")
        await seed_instance(instance_key="other", provider_account_id=ACCOUNT, status="online")

        updated = await adapter_instance_service.set_status(live, "online", message="连接就绪")

        assert updated.provider_account_id == ACCOUNT
        assert updated.last_error == ""
        assert await live_account_holders() == [ADMIN_KEY, "other"]

    @pytest.mark.parametrize("status", ["disabled", "archived"])
    async def test_enabling_a_non_tombstone_row_never_releases_its_account(self, db, status: str) -> None:
        """停用/归档的复原**不是**账号写入路径，判据必须与 `set_status` 那道守卫逐字相同

        这两个状态从来没有停止算账号占用（`find_account_binding_conflict` 只排除墓碑），
        所以它们身上出现冲突只可能是脏数据；而脏数据该谁让位由
        `unbind_duplicate_account_bindings` 说了算——它保留**最早那行**，正是这里被启用的这一行、
        也正是 `get_by_bound_account` 此刻在路由的那一行。

        放宽到这两个状态时实测的后果正相反：管理员点一下「启用」，这一行自己的账号被解掉，
        入站消息静默改判给另一行——**启用一个实例反而让它彻底收不到消息**。
        """
        stale = await seed_instance(instance_key=ADMIN_KEY, provider_account_id=ACCOUNT, status=status)
        await seed_instance(instance_key="other", provider_account_id=ACCOUNT, status="online")
        routed_before = await DBAdapterInstance.get_by_bound_account(ADAPTER, ACCOUNT)
        assert routed_before is not None and routed_before.instance_key == ADMIN_KEY

        revived = await set_instance_enabled(stale, enabled=True)

        assert revived.provider_account_id == ACCOUNT
        assert revived.last_error == ""
        routed_after = await DBAdapterInstance.get_by_bound_account(ADAPTER, ACCOUNT)
        assert routed_after is not None and routed_after.instance_key == ADMIN_KEY

    async def test_releasing_an_account_invalidates_the_inbound_routing_cache(self, db) -> None:
        """解绑也是一次绑定关系变更，`账号 -> 实例` 缓存必须当场作废

        这个缓存没有 TTL：留一条旧映射，该账号的消息会一直投给旧实例直到进程重启，且毫无告警
        （判据与 `test_napcat_login_account_writeback` 里钉住回写侧的那条同源）。
        变异测试逼出来的：删掉 `clear_instance_cache()` 后本轮新增的用例全绿。
        """
        tomb = await self._tombstone_whose_account_was_taken()
        # 手工种一条陈旧映射，模拟"这个账号在删除之前被解析到墓碑那一行上过"
        instance_module._account_to_instance[ACCOUNT] = "dead"  # noqa: SLF001
        instance_module._instance_to_account["dead"] = ACCOUNT  # noqa: SLF001

        await set_instance_enabled(tomb, enabled=True)

        assert instance_module._account_to_instance == {}  # noqa: SLF001
        assert instance_module.lookup_cached_account_id("dead") is None

    async def test_releasing_an_account_leaves_an_audit_row(self, db) -> None:
        """解绑是一次没有人按下按钮的写入，事件表是事后唯一能回答"谁动的"的地方"""
        tomb = await self._tombstone_whose_account_was_taken()

        await set_instance_enabled(tomb, enabled=True)

        events = await DBAdapterInstanceEvent.filter(instance_id=tomb.id, event_type="account_released")
        assert len(events) == 1
        assert ACCOUNT in events[0].message
        assert ADMIN_KEY in events[0].message

    async def test_idempotent_re_registration_revives_without_duplicating_the_binding(self, db) -> None:
        """`adapter_instance_service.create_instance` 的幂等复活是第二条复活路径

        `adapters/wechat_ilink_multi` 的登记接口靠它把墓碑占住的 `(adapter_key, instance_key)`
        释放出来，因此这条路不能改成"拒绝复活"，只能解绑陈旧账号。
        """
        await self._tombstone_whose_account_was_taken()

        revived = await adapter_instance_service.create_instance(ADAPTER, "dead", "dead", "onebot_v11")

        assert revived.status == "pending"
        assert revived.provider_account_id == ""
        assert await live_account_holders() == [ADMIN_KEY]

    async def test_inbound_routing_does_not_flip_to_the_revived_shell(self, db) -> None:
        """这才是这一组存在的理由：改前复活会把入站消息从 inst1 静默改判给 dead

        `get_by_bound_account` 取最早那行，而墓碑的 id 通常比接手的实例小。
        """
        tomb = await self._tombstone_whose_account_was_taken()
        await set_instance_enabled(tomb, enabled=True)

        instance_module.clear_instance_cache()
        assert await instance_module.resolve_instance_key(ACCOUNT) == ADMIN_KEY


class TestAutoRegistrationNeverLandsOnADeadOrTakenKey:
    """协议端自行连入是第四条写入路径，它按**实例标识**取行，不看账号占用判据"""

    async def test_a_tombstone_is_never_reused_as_the_inbound_target(self, db) -> None:
        """改前：叫 `ob{QQ}` 的墓碑记着同一个账号，被原样取回来当成"就是它"

        结果是该账号的消息全部投给一个 `deleted` 实例 —— `is_instance_active` 静默丢掉它们，
        而总后台列表把墓碑过滤掉了，现场连这一行都看不见。
        """
        await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="deleted")

        resolved = await instance_module.resolve_instance_key(ACCOUNT)

        assert resolved != AUTO_KEY
        registered = await DBAdapterInstance.get(instance_key=resolved)
        assert registered.status == "unclaimed"
        assert await DBAdapterInstance.is_instance_active(ADAPTER, resolved) is True
        assert await live_account_holders() == [resolved]

    async def test_taken_fallback_key_does_not_crash_the_inbound_chain(self, db) -> None:
        """改前退让标识 `ob{QQ}x` 是直接 `create`：它也被占着就抛 `IntegrityError`

        爆炸半径与 #31 原本那条 `MultipleObjectsReturned` 完全相同——入站链路第一步炸掉，
        该账号一条消息都进不来。
        """
        await seed_instance(instance_key=AUTO_KEY, provider_account_id="")
        await seed_instance(instance_key=f"{AUTO_KEY}x", provider_account_id="")

        resolved = await instance_module.resolve_instance_key(ACCOUNT)

        assert resolved == f"{AUTO_KEY}x2"
        assert await live_account_holders() == [resolved]

    @pytest.mark.parametrize(
        ("occupied", "expected"),
        [
            ((), AUTO_KEY),
            ((AUTO_KEY,), f"{AUTO_KEY}x"),
            ((AUTO_KEY, f"{AUTO_KEY}x"), f"{AUTO_KEY}x2"),
        ],
    )
    async def test_candidate_order_keeps_the_historical_two(self, db, occupied, expected: str) -> None:
        """前两个候选与改造前逐字相同，常见路径上的实例标识因此不变"""
        for index, key in enumerate(occupied):
            await seed_instance(instance_key=key, provider_account_id=f"9999{index}")

        assert await instance_module.resolve_instance_key(ACCOUNT) == expected

    async def test_registration_is_idempotent_across_cold_caches(self, db) -> None:
        """第二条消息必须落回同一行，否则每次冷缓存都多一个未认领实例"""
        first = await instance_module.resolve_instance_key(ACCOUNT)
        instance_module.clear_instance_cache()
        second = await instance_module.resolve_instance_key(ACCOUNT)

        assert first == second == AUTO_KEY
        assert await DBAdapterInstance.all().count() == 1

    async def test_losing_the_insert_race_falls_back_to_the_row_that_won(self, db, monkeypatch) -> None:
        """并发下输掉插入竞争的那一条消息，必须落回赢家那一行，而不是抛出去

        改造前这半个保证由 `get_or_create` 提供；换成"先查后建"之后它只剩下
        `IntegrityError` 之后的那次回读。这条分支在真库上（真唯一约束、真 `IntegrityError`）
        才跑得到，因此这里**只伪造那一个交错**：让候选标识的第一次存在性查询谎报"不存在"，
        插入、唯一约束、异常类型、回读与随后的判据全部是真的。

        变异测试逼出来的：把这条分支改成 `raise`、或者删掉"标识上已经是同一个账号就用它"
        这半条判据，本文件在补这条用例之前全绿。
        """
        real_get_or_none = DBAdapterInstance.get_or_none
        raced = {"done": False}

        async def concurrent_insert_lands_right_here(*args, **kwargs):
            """赢家那一行恰好在"我查过了、还没插"这个窗口里落库"""
            if not raced["done"] and kwargs.get("instance_key") == AUTO_KEY:
                raced["done"] = True
                await seed_instance(instance_key=AUTO_KEY, provider_account_id=ACCOUNT, status="unclaimed")
                return None
            return await real_get_or_none(*args, **kwargs)

        monkeypatch.setattr(DBAdapterInstance, "get_or_none", concurrent_insert_lands_right_here)

        resolved = await instance_module.resolve_instance_key(ACCOUNT)

        assert raced["done"] is True, "没走到插入竞争那一步，这条用例没有测到它要测的东西"
        assert resolved == AUTO_KEY
        # 输掉竞争的一方不许另起一行：两行活着的同账号实例正是 #31 本身
        assert await live_account_holders() == [AUTO_KEY]
        assert await DBAdapterInstance.all().count() == 1

    async def test_exhausted_candidates_fail_loudly_instead_of_writing_a_duplicate(self, db) -> None:
        """候选耗尽时宁可抛，也不能静默复用一个已被占用的标识

        上界存在只是为了不让"找不到空位"退化成死循环。真耗光要有人把 `ob{QQ}` 与它全部
        退让标识都建成别的实例——那种库已经足够反常，静默落到别人的实例上会把这个账号的消息
        整条投给一个毫不相干的实例，而错误里连一句线索都没有。
        """
        occupied = [AUTO_KEY, *(f"{AUTO_KEY}x" if i == 1 else f"{AUTO_KEY}x{i}" for i in range(1, 10))]
        for index, key in enumerate(occupied):
            await seed_instance(instance_key=key, provider_account_id=f"7000{index}")

        with pytest.raises(ValueError) as excinfo:
            await instance_module.resolve_instance_key(ACCOUNT)

        assert ACCOUNT in str(excinfo.value)
        assert await DBAdapterInstance.all().count() == len(occupied)
        assert await live_account_holders() == []
