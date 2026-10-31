"""仪表盘统计与全局事件流的租户作用域

这两族端点上一轮被有意留在 `UNSCOPED_TODO` 里，理由是"半吊子守卫比没有更危险"：

- `/dashboard/*` 是前端的默认落地页（`Navigate to="/dashboard"`，路由本身无角色守卫）。
  提到 `Role.Super` 会直接打掉租户与只读成员的首页，所以唯一正确的解法是**按实例过滤聚合查询**。
- `/events/stream` 建连即推全域快照，之后每个事件也是全域广播。订阅位是全局稀缺资源
  （`_MAX_SUBSCRIBERS=50`），不能按租户各开一路订阅，只能在**订阅者自己的出口**逐条过滤。

本文件钉住四件事：

1. 七处聚合查询全部收进当前身份的实例范围，租户拿不到别人的消息数、执行数、排行榜姓名；
2. 实时统计流的**历史桶缓存按作用域分键** —— 不分键的话第一个租户查过的桶会被别人直接复用，
   这个纯性能缓存本身就成了一条跨租户读取通道；
3. 事件流按事件自带的 `workspace_id` / `chat_key` / `instance_key` 逐条过滤，快照按 domain 裁剪；
4. **未登记可见性判据的事件类型对租户不可见**。新增一种广播时忘了登记，
   后果必须是"租户看不到新功能"而不是"租户看到全域数据"，所以这里同时钉住
   `SystemEvent` 联合类型里的每个 `type` 都出现在 `routers/events.py` 的判据表里。

平台方与未启用多租户的部署一个分支都不进，行为与改造前逐字一致。
"""

import datetime
import json
from typing import Any, Dict, List, Optional, Set, Tuple, get_args

import pytest
from fastapi import Request
from sse_starlette.sse import EventSourceResponse
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.routers.dashboard import (
    _fetch_stream_bucket,
    get_dashboard_overview,
    get_distributions,
    get_ranking,
    get_stats_stream,
    get_trends,
    resolve_dashboard_scope,
)
from nekro_agent.routers.events import (
    CHAT_SCOPED_EVENTS,
    INSTANCE_SCOPED_EVENTS,
    SHARED_LIBRARY_EVENTS,
    WORKSPACE_AND_CHAT_SCOPED_EVENTS,
    WORKSPACE_SCOPED_EVENTS,
    resolve_event_visibility,
    stream_system_events,
)
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import system_broadcast
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
EMPTY_TENANT_ID = 3

ACME_CHAT_KEY = "onebot_v11-inst1-group_10001"
ACME_SECOND_CHAT_KEY = "onebot_v11-inst2-group_10002"
RIVAL_CHAT_KEY = "onebot_v11-rival1-group_20001"
UNOWNED_CHAT_KEY = "onebot_v11-ob1000000003-group_30001"

ACME_WORKSPACE_ID = 1
RIVAL_WORKSPACE_ID = 2

# 每个频道播下的消息条数与沙盒执行条数，取值互不相同，
# 好让"是不是把别人的也算进来了"在断言里一眼可见。
SEEDED_MESSAGES = {ACME_CHAT_KEY: 3, ACME_SECOND_CHAT_KEY: 2, RIVAL_CHAT_KEY: 5, UNOWNED_CHAT_KEY: 7}
SEEDED_EXECS = {ACME_CHAT_KEY: 4, ACME_SECOND_CHAT_KEY: 1, RIVAL_CHAT_KEY: 6, UNOWNED_CHAT_KEY: 2}

ACME_TOTAL_MESSAGES = SEEDED_MESSAGES[ACME_CHAT_KEY] + SEEDED_MESSAGES[ACME_SECOND_CHAT_KEY]
ACME_TOTAL_EXECS = SEEDED_EXECS[ACME_CHAT_KEY] + SEEDED_EXECS[ACME_SECOND_CHAT_KEY]
ALL_MESSAGES = sum(SEEDED_MESSAGES.values())
ALL_EXECS = sum(SEEDED_EXECS.values())


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_fixture_data()
        yield
    finally:
        await Tortoise.close_connections()


async def seed_fixture_data() -> None:
    """两个租户各一批实例/频道/消息/执行记录，外加一个未归属实例

    形态取自测试环境：`acme_ltd` 名下 `inst1`/`inst2`，另有 `ob1000000003` 未归属任何租户。
    工作区按频道绑定推导归属（工作区表本身没有租户列），所以这里给两侧各绑一个。
    """
    layout: List[Tuple[str, Optional[int], str, Optional[int]]] = [
        ("inst1", ACME_TENANT_ID, ACME_CHAT_KEY, ACME_WORKSPACE_ID),
        ("inst2", ACME_TENANT_ID, ACME_SECOND_CHAT_KEY, None),
        ("rival1", RIVAL_TENANT_ID, RIVAL_CHAT_KEY, RIVAL_WORKSPACE_ID),
        ("ob1000000003", None, UNOWNED_CHAT_KEY, None),
    ]
    for workspace_id in (ACME_WORKSPACE_ID, RIVAL_WORKSPACE_ID):
        await DBWorkspace.create(id=workspace_id, name=f"ws{workspace_id}", container_name=f"nekro-cc-{workspace_id}")

    for instance_key, tenant_id, chat_key, workspace_id in layout:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
            chat_key=chat_key,
            channel_type="group",
            workspace_id=workspace_id,
            data="{}",
        )
        for index in range(SEEDED_MESSAGES[chat_key]):
            await DBChatMessage.create(
                sender_id=f"{instance_key}-sender-{index}",
                sender_name=f"{instance_key}-name-{index}",
                sender_nickname=f"{instance_key}-nick-{index}",
                is_tome=0,
                is_recalled=False,
                adapter_key="onebot_v11",
                instance_key=instance_key,
                message_id=f"{instance_key}-msg-{index}",
                chat_key=chat_key,
                # 生产链路把 `ChatType` 枚举直接交给 CharField，落库形态是 `str(枚举)`，
                # 这里必须照抄，否则分布统计的过滤条件对不上，测的就不是真实数据。
                chat_type=ChatType.GROUP,
                platform_userid=f"{instance_key}-{index}",
                content_text="hi",
                content_data="[]",
                raw_cq_code="",
                ext_data="",
                send_timestamp=0,
            )
        for index in range(SEEDED_EXECS[chat_key]):
            await DBExecCode.create(
                chat_key=chat_key,
                trigger_user_id=f"{instance_key}-user",
                trigger_user_name=f"{instance_key}-昵称",
                success=index % 2 == 0,
                code_text="print(1)",
                outputs="1",
                stop_type=ExecStopType.NORMAL,
                exec_time_ms=100,
            )


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份

    作用域判定只读 `tenant_id` / `tenant_role` / `perm_level`，落库与否不影响判定。
    """
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


def acme_member() -> DBUser:
    """租户里的只读成员：仪表盘是它的落地页，必须仍然能读到自己租户的数据"""
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def empty_tenant_admin() -> DBUser:
    """名下一个实例都没有的租户：统计必须是 0，而不是退化成不受限"""
    return make_user(tenant_id=EMPTY_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


def make_request() -> Request:
    """一个最小可用的 ASGI 请求

    两条 SSE 端点都在建流之前完成身份判定，此时请求对象还没被读过，
    因此一个只带必需字段的 scope 足以覆盖被测路径。
    """
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


async def backdate_messages(chat_key: str, count: int, moment: datetime.datetime) -> None:
    """把若干条消息的落库时间挪进指定时刻

    `create_time` 是 `auto_now_add`，只能落库后再改；历史桶的断言需要桶里真的有数据，
    否则"缓存串了没串"两种情况都是 0，测试等于没测。
    """
    ids = await DBChatMessage.filter(chat_key=chat_key).order_by("id").limit(count).values_list("id", flat=True)
    await DBChatMessage.filter(id__in=list(ids)).update(create_time=moment)


class TestDashboardOverviewIsScopedToOwnInstances:
    """概览的五个计数全部按实例收口"""

    @pytest.mark.parametrize(
        ("identity", "expected_messages", "expected_execs"),
        [
            pytest.param(platform_owner, ALL_MESSAGES, ALL_EXECS, id="platform-owner"),
            pytest.param(single_tenant_admin, ALL_MESSAGES, ALL_EXECS, id="single-tenant"),
            pytest.param(acme_admin, ACME_TOTAL_MESSAGES, ACME_TOTAL_EXECS, id="acme-admin"),
            pytest.param(acme_member, ACME_TOTAL_MESSAGES, ACME_TOTAL_EXECS, id="acme-readonly-member"),
            pytest.param(rival_admin, SEEDED_MESSAGES[RIVAL_CHAT_KEY], SEEDED_EXECS[RIVAL_CHAT_KEY], id="rival-admin"),
            pytest.param(empty_tenant_admin, 0, 0, id="tenant-without-instances"),
        ],
    )
    async def test_counts_match_the_identity(
        self,
        db,  # noqa: ARG002
        identity,
        expected_messages: int,
        expected_execs: int,
    ) -> None:
        overview = await get_dashboard_overview(time_range="day", _current_user=identity())
        assert overview.total_messages == expected_messages
        assert overview.total_sandbox_calls == expected_execs

    async def test_distinct_counts_are_scoped(self, db) -> None:  # noqa: ARG002
        """COUNT(DISTINCT) 也要过滤：会话数与去重用户数同样是跨租户信号"""
        acme = await get_dashboard_overview(time_range="day", _current_user=acme_admin())
        everything = await get_dashboard_overview(time_range="day", _current_user=platform_owner())
        assert acme.active_sessions == 2
        assert acme.unique_users == ACME_TOTAL_MESSAGES
        assert everything.active_sessions == len(SEEDED_MESSAGES)
        assert everything.unique_users == ALL_MESSAGES


class TestRankingDoesNotLeakForeignNames:
    """排行榜条目直接带着 `trigger_user_name`，是最直白的一条泄露路径"""

    async def test_tenant_sees_only_own_triggers(self, db) -> None:  # noqa: ARG002
        rows = await get_ranking(ranking_type="users", time_range="day", limit=10, _current_user=acme_admin())
        assert {row.id for row in rows} == {"inst1-user", "inst2-user"}
        assert {row.value for row in rows} == {SEEDED_EXECS[ACME_CHAT_KEY], SEEDED_EXECS[ACME_SECOND_CHAT_KEY]}

    async def test_platform_owner_still_sees_everyone(self, db) -> None:  # noqa: ARG002
        rows = await get_ranking(ranking_type="users", time_range="day", limit=10, _current_user=platform_owner())
        assert {row.id for row in rows} == {"inst1-user", "inst2-user", "rival1-user", "ob1000000003-user"}

    async def test_tenant_without_instances_gets_nothing(self, db) -> None:  # noqa: ARG002
        rows = await get_ranking(ranking_type="users", time_range="day", limit=10, _current_user=empty_tenant_admin())
        assert rows == []


class TestDistributionsAndTrendsAreScoped:
    async def test_distribution_totals_match_own_data(self, db) -> None:  # noqa: ARG002
        acme = await get_distributions(time_range="day", _current_user=acme_admin())
        assert sum(item.value for item in acme.stop_type) == ACME_TOTAL_EXECS
        assert sum(item.value for item in acme.message_type) == ACME_TOTAL_MESSAGES

    async def test_platform_owner_totals_cover_everything(self, db) -> None:  # noqa: ARG002
        everything = await get_distributions(time_range="day", _current_user=platform_owner())
        assert sum(item.value for item in everything.stop_type) == ALL_EXECS
        assert sum(item.value for item in everything.message_type) == ALL_MESSAGES

    async def test_trends_sum_to_own_totals(self, db) -> None:  # noqa: ARG002
        points = await get_trends(
            metrics="messages,sandbox_calls",
            time_range="day",
            interval="hour",
            _current_user=acme_admin(),
        )
        assert sum(int(point["messages"]) for point in points) == ACME_TOTAL_MESSAGES
        assert sum(int(point["sandbox_calls"]) for point in points) == ACME_TOTAL_EXECS


class TestStreamBucketCacheIsKeyedByScope:
    """历史桶缓存不分作用域的话，它本身就是一条跨租户读取通道"""

    async def test_second_scope_does_not_reuse_the_first_result(self, db) -> None:  # noqa: ARG002
        bucket_end = db_now() - datetime.timedelta(minutes=20)
        bucket_start = bucket_end - datetime.timedelta(minutes=10)
        inside = bucket_start + datetime.timedelta(minutes=5)
        await backdate_messages(ACME_CHAT_KEY, 1, inside)
        await backdate_messages(RIVAL_CHAT_KEY, 2, inside)

        platform_scope = await resolve_dashboard_scope(platform_owner())
        acme_scope = await resolve_dashboard_scope(acme_admin())
        assert platform_scope.cache_tag != acme_scope.cache_tag

        # 先让平台方把这个已经过去的桶写进缓存，再用租户身份查同一个桶
        platform_bucket = await _fetch_stream_bucket(platform_scope, bucket_start, bucket_end)
        tenant_bucket = await _fetch_stream_bucket(acme_scope, bucket_start, bucket_end)
        assert platform_bucket["recent_messages"] == 3
        assert tenant_bucket["recent_messages"] == 1

    async def test_two_tenants_get_distinct_cache_tags(self, db) -> None:  # noqa: ARG002
        acme_scope = await resolve_dashboard_scope(acme_admin())
        rival_scope = await resolve_dashboard_scope(rival_admin())
        empty_scope = await resolve_dashboard_scope(empty_tenant_admin())
        assert len({acme_scope.cache_tag, rival_scope.cache_tag, empty_scope.cache_tag}) == 3

    async def test_stats_stream_resolves_scope_before_building_the_stream(self, db) -> None:  # noqa: ARG002
        """建流本身不该 500：`EventSourceResponse` 一返回就已经是 200，之后再抛异常看不出来"""
        response = await get_stats_stream(request=make_request(), granularity=10, _current_user=acme_admin())
        assert isinstance(response, EventSourceResponse)


class TestExecCodeOwnershipComesFromTheChannelTable:
    """`exec_code` 只有 `chat_key`，归属只能经 `chat_channel` 一跳推出来

    历史上这里写过 `{adapter_key}-{instance_key}-` 前缀匹配。那个格式只是
    `BaseAdapter.build_chat_key` 的默认实现：`wechat_ilink_multi` 落成
    `wxim-{instance_key}:group:{id}`、`qqbot_openclaw` 落成 `qqoc-{channel_id}`（没有实例段），
    两者都在 `adapters/__init__.py:CHAT_KEY_PREFIX_ALIASES` 里有登记。
    前缀匹配对它们恒不命中，租户会在自己的首页上看到"有消息、零沙盒执行"这种自相矛盾的数字。
    """

    async def test_short_prefix_adapter_channels_are_counted(self, db) -> None:  # noqa: ARG002
        """短前缀适配器（chat_key 不含 `{adapter_key}-{instance_key}-` 段）必须算进自己人"""
        await DBAdapterInstance.create(
            adapter_key="wechat_ilink_multi",
            instance_key="wxa",
            display_name="wxa",
            status="online",
            provider="remote_onebot",
            tenant_id=ACME_TENANT_ID,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key="wechat_ilink_multi",
            instance_key="wxa",
            channel_id="wxa:group:g1",
            chat_key="wxim-wxa:group:g1",
            channel_type="group",
            data="{}",
        )
        for _ in range(3):
            await DBExecCode.create(chat_key="wxim-wxa:group:g1", code_text="", outputs="")

        scope = await resolve_dashboard_scope(acme_admin())
        assert await scope.exec_codes().count() == ACME_TOTAL_EXECS + 3

    async def test_foreign_and_orphan_chat_keys_stay_invisible(self, db) -> None:  # noqa: ARG002
        """别的租户的频道、以及没有频道行的孤儿执行记录，一律不可见"""
        await DBExecCode.create(chat_key="onebot_v11-ghost-group_1", code_text="", outputs="")
        scope = await resolve_dashboard_scope(acme_admin())
        assert sorted(set(await scope.exec_codes().values_list("chat_key", flat=True))) == [
            ACME_CHAT_KEY,
            ACME_SECOND_CHAT_KEY,
        ]

    async def test_similar_instance_key_does_not_widen_the_scope(self, db) -> None:  # noqa: ARG002
        """`inst1` 不能顺带吃到 `inst10` / `inst_1` 的执行记录"""
        for instance_key in ("inst10", "inst_1"):
            await DBAdapterInstance.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=RIVAL_TENANT_ID,
                metadata_json="",
            )
            chat_key = f"onebot_v11-{instance_key}-group_1"
            await DBChatChannel.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                channel_id="group_1",
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )
            await DBExecCode.create(chat_key=chat_key, code_text="", outputs="")

        scope = await resolve_dashboard_scope(acme_admin())
        assert await scope.exec_codes().count() == ACME_TOTAL_EXECS


class TestPlatformSideIsUnfiltered:
    """平台方与未启用多租户的部署一个过滤分支都不该进"""

    @pytest.mark.parametrize("identity", [platform_owner, single_tenant_admin], ids=["platform-owner", "single-tenant"])
    async def test_scope_is_unrestricted(self, db, identity) -> None:  # noqa: ARG002
        scope = await resolve_dashboard_scope(identity())
        assert scope.instance_keys is None
        assert await scope.exec_codes().count() == ALL_EXECS
        assert await scope.messages().count() == ALL_MESSAGES

    @pytest.mark.parametrize("identity", [platform_owner, single_tenant_admin], ids=["platform-owner", "single-tenant"])
    async def test_event_stream_is_unfiltered(self, db, identity) -> None:  # noqa: ARG002
        assert await resolve_event_visibility(identity()) is None


def workspace_event(workspace_id: int) -> Dict[str, Any]:
    return {"type": "workspace_status", "workspace_id": workspace_id, "status": "active", "name": "ws"}


def agent_event(chat_key: str) -> Dict[str, Any]:
    return {"type": "agent_active", "chat_key": chat_key, "active": True, "started_at": 0, "max_duration_ms": 1000}


async def visible(user: DBUser, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    visibility = await resolve_event_visibility(user)
    assert visibility is not None
    payload = await visibility.visible_payload(json.dumps(event))
    return None if payload is None else json.loads(payload)


class TestEventStreamFiltersPerEvent:
    """逐事件过滤：判据来自事件自带的归属字段"""

    async def test_own_workspace_event_passes(self, db) -> None:  # noqa: ARG002
        assert await visible(acme_admin(), workspace_event(ACME_WORKSPACE_ID)) is not None

    async def test_foreign_workspace_event_is_dropped(self, db) -> None:  # noqa: ARG002
        assert await visible(acme_admin(), workspace_event(RIVAL_WORKSPACE_ID)) is None

    async def test_own_channel_event_passes(self, db) -> None:  # noqa: ARG002
        assert await visible(acme_admin(), agent_event(ACME_SECOND_CHAT_KEY)) is not None

    @pytest.mark.parametrize("chat_key", [RIVAL_CHAT_KEY, UNOWNED_CHAT_KEY], ids=["other-tenant", "unassigned"])
    async def test_foreign_channel_event_is_dropped(self, db, chat_key: str) -> None:  # noqa: ARG002
        assert await visible(acme_admin(), agent_event(chat_key)) is None

    async def test_instance_prefix_is_not_a_substring_match(self, db) -> None:  # noqa: ARG002
        """`inst1` 不能顺带匹配上 `inst10` 的频道"""
        assert await visible(acme_admin(), agent_event("onebot_v11-inst10-group_1")) is None

    async def test_short_prefix_adapter_channel_is_visible_to_its_owner(self, db) -> None:  # noqa: ARG002
        """`wxim-{instance_key}:group:{id}` 里没有 `{adapter_key}-{instance_key}-` 段

        归属只能查 `chat_channel`。按前缀判会让租户永远看不到自己微信频道的 Agent 活动。
        """
        await DBAdapterInstance.create(
            adapter_key="wechat_ilink_multi",
            instance_key="wxa",
            display_name="wxa",
            status="online",
            provider="remote_onebot",
            tenant_id=ACME_TENANT_ID,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key="wechat_ilink_multi",
            instance_key="wxa",
            channel_id="wxa:group:g1",
            chat_key="wxim-wxa:group:g1",
            channel_type="group",
            data="{}",
        )
        assert await visible(acme_admin(), agent_event("wxim-wxa:group:g1")) is not None
        assert await visible(rival_admin(), agent_event("wxim-wxa:group:g1")) is None

    async def test_instance_status_event_follows_instance_ownership(self, db) -> None:  # noqa: ARG002
        own = {"type": "adapter_instance_status", "adapter_key": "onebot_v11", "instance_key": "inst1", "status": "online"}
        foreign = {**own, "instance_key": "rival1"}
        assert await visible(acme_admin(), own) is not None
        assert await visible(acme_admin(), foreign) is None

    async def test_memory_recall_needs_both_workspace_and_channel(self, db) -> None:  # noqa: ARG002
        """工作区归属不唯一或不属于自己时，哪怕频道是自己的也不能推"""
        base = {
            "type": "memory_recall_activity",
            "active": True,
            "phase": "applied",
            "request_id": "r1",
            "target_kind": "na_history",
        }
        assert await visible(acme_admin(), {**base, "workspace_id": ACME_WORKSPACE_ID, "chat_key": ACME_CHAT_KEY}) is not None
        assert await visible(acme_admin(), {**base, "workspace_id": RIVAL_WORKSPACE_ID, "chat_key": ACME_CHAT_KEY}) is None
        assert await visible(acme_admin(), {**base, "workspace_id": ACME_WORKSPACE_ID, "chat_key": RIVAL_CHAT_KEY}) is None

    async def test_unregistered_event_type_is_invisible_to_tenants(self, db) -> None:  # noqa: ARG002
        """默认拒绝：漏登记的后果必须是"看不到"，不是"看到全部" """
        assert await visible(acme_admin(), {"type": "brand_new_broadcast", "workspace_id": ACME_WORKSPACE_ID}) is None


class TestEventStreamPrunesTheSnapshot:
    """建连快照是这条流最大的一次泄露：它一口气带出全部工作区 ID/名称与全部活动频道"""

    async def test_snapshot_keeps_only_own_entries(self, db) -> None:  # noqa: ARG002
        snapshot = {
            "type": "snapshot",
            "data": {
                "workspace_status": {
                    str(ACME_WORKSPACE_ID): {"workspace_id": ACME_WORKSPACE_ID, "status": "active", "name": "acme-ws"},
                    str(RIVAL_WORKSPACE_ID): {"workspace_id": RIVAL_WORKSPACE_ID, "status": "active", "name": "rival-ws"},
                },
                "agent_active": {
                    ACME_CHAT_KEY: {"chat_key": ACME_CHAT_KEY, "active": True},
                    RIVAL_CHAT_KEY: {"chat_key": RIVAL_CHAT_KEY, "active": True},
                },
            },
        }
        pruned = await visible(acme_admin(), snapshot)
        assert pruned is not None
        assert pruned["type"] == "snapshot"
        assert list(pruned["data"]["workspace_status"]) == [str(ACME_WORKSPACE_ID)]
        assert list(pruned["data"]["agent_active"]) == [ACME_CHAT_KEY]
        assert "rival-ws" not in json.dumps(pruned, ensure_ascii=False)

    async def test_snapshot_is_still_delivered_when_everything_is_filtered_out(self, db) -> None:  # noqa: ARG002
        """空快照也要发：前端靠它初始化各张状态表，吞掉会让页面永远停在加载态"""
        snapshot = {
            "type": "snapshot",
            "data": {"workspace_status": {str(RIVAL_WORKSPACE_ID): {"workspace_id": RIVAL_WORKSPACE_ID}}},
        }
        pruned = await visible(empty_tenant_admin(), snapshot)
        assert pruned == {"type": "snapshot", "data": {}}


def broadcast_event_kinds() -> Set[str]:
    """`SystemEvent` 联合类型里每个成员的 `type` 取值"""
    union = get_args(system_broadcast.SystemEvent)[0]
    return {str(member.model_fields["type"].default) for member in get_args(union)}


class TestEveryBroadcastKindIsRegistered:
    """新增一种广播时忘了登记判据，必须在这里红，而不是在生产上静默泄露"""

    def test_all_event_types_have_a_visibility_rule(self) -> None:
        registered = (
            WORKSPACE_SCOPED_EVENTS
            | CHAT_SCOPED_EVENTS
            | WORKSPACE_AND_CHAT_SCOPED_EVENTS
            | INSTANCE_SCOPED_EVENTS
            | SHARED_LIBRARY_EVENTS
        )
        assert broadcast_event_kinds() - registered == set()

    def test_rules_do_not_overlap(self) -> None:
        """一个类型落进两张表说明判据含糊，`_allows` 的分支顺序会决定结果"""
        buckets = [
            WORKSPACE_SCOPED_EVENTS,
            CHAT_SCOPED_EVENTS,
            WORKSPACE_AND_CHAT_SCOPED_EVENTS,
            INSTANCE_SCOPED_EVENTS,
            SHARED_LIBRARY_EVENTS,
        ]
        total = sum(len(bucket) for bucket in buckets)
        assert len(set().union(*buckets)) == total

    async def test_snapshot_domains_are_the_event_type_names(self, monkeypatch) -> None:
        """快照裁剪与事件过滤共用一张判据表，前提是 domain 名 == 事件 `type` 名

        这个等式写在 `system_broadcast._update_state` 里，一旦有人给某个 domain 换个名字，
        对应的快照条目就会掉进"未登记"分支被静默丢弃，租户的页面上少一块东西而没有任何信号。
        """
        monkeypatch.setattr(system_broadcast, "_state_store", {})
        monkeypatch.setattr(system_broadcast, "_subscribers", [])
        for event in [
            system_broadcast.WorkspaceStatusEvent(workspace_id=1, status="active", name="ws"),
            system_broadcast.WorkspaceCcActiveEvent(workspace_id=1, active=True),
            system_broadcast.WorkspaceCcRuntimeStatusEvent(workspace_id=1, active=True),
            system_broadcast.AgentActiveEvent(chat_key=ACME_CHAT_KEY, active=True),
            system_broadcast.AgentRuntimeStatusEvent(chat_key=ACME_CHAT_KEY, active=True),
            system_broadcast.MemoryRecallActivityEvent(workspace_id=1, chat_key=ACME_CHAT_KEY, active=True, request_id="r"),
            system_broadcast.KbIndexProgressEvent(workspace_id=1, document_id=1, active=True),
            system_broadcast.KbLibraryIndexProgressEvent(asset_id=1, active=True),
            system_broadcast.AdapterInstanceStatusEvent(adapter_key="onebot_v11", instance_key="inst1", status="online"),
        ]:
            await system_broadcast.publish_system_event(event)
        assert set(system_broadcast.get_state_snapshot()) == broadcast_event_kinds()


class TestEventStreamStillBuildsForTenants:
    """建流本身不能 500：`@require_role` 只在参数名叫 `_current_user` 时生效，
    装饰器在、契约测试过、端点却坏掉是本项目真踩过的形态。"""

    @pytest.mark.parametrize("identity", [platform_owner, acme_admin], ids=["platform-owner", "acme-admin"])
    async def test_stream_returns_an_event_source_response(self, db, identity, monkeypatch) -> None:  # noqa: ARG002
        monkeypatch.setattr(system_broadcast, "_state_store", {})
        monkeypatch.setattr(system_broadcast, "_subscribers", [])
        response = await stream_system_events(request=make_request(), _current_user=identity())
        assert isinstance(response, EventSourceResponse)
