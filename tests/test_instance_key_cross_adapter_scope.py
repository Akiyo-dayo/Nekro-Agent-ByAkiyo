"""同名实例跨适配器分属两个租户时的归属判定

## 要钉住的形态

`adapter_instance` 的唯一约束是 `(adapter_key, instance_key)`，`instance_key` 只在同一适配器内唯一。
于是下面这份数据是合法的：

    租户 acme  名下 onebot_v11/inst1          （频道 onebot_v11-inst1-group_1001）
    租户 rival 名下 wechat_ilink_multi/inst1  （频道 wxim-inst1:group:2002）

改前的各处归属判定只看 `instance_key`：

- 作用域过滤用 `instance_key__in=[租户名下的标识]`，两个租户都看得到对方的频道、消息、公告、工作区；
- 归属守卫按标识随便取一行再比租户，取到哪行取决于行号：行号小的那个租户能写另一个租户的
  实例覆盖配置与频道配置、能把公告发进对方的群；另一个租户连自己的实例都被判成不存在；
- 从频道反推租户（技能库作用域、模型组运行时兜底、配额上限、工作区网络分组）同样会推到另一行上。

## 为什么每条用例都在两种行序下跑

"按标识取第一行"的错误只在**别人那行行号更小**时显形。只测一种行序，修错一半（例如只修了
"取到自己那行"的方向）也会全绿；两种行序都跑，任何一处退回按标识判定都至少在其中一种下变红。

## 数据怎么来的

这份数据由真实路径写得出来：适配器自己的登记接口（`AdapterInstanceService.create_instance`）
此前只查 `(adapter_key, instance_key)`。现在它与总后台一样拒绝跨适配器重名（见 `TestCreationGuard`），
但存量库里可能已经有这种行，所以下面直接落库，守卫必须对它们同样正确。
"""

import ast
import datetime
import inspect
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters import ADAPTER_REGISTRY
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_announcement import AnnouncementScopeType, AnnouncementStatus, DBAnnouncement
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.identity_scope import ScopeDomain
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import accessible_instance_refs, assert_instance_accessible

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
BYSTANDER_TENANT_ID = 3

ONEBOT = "onebot_v11"
WECHAT = "wechat_ilink_multi"
SHARED_KEY = "inst1"

ACME_CHAT_KEY = "onebot_v11-inst1-group_1001"
RIVAL_CHAT_KEY = "wxim-inst1:group:2002"

ACME_WORKSPACE_ID = 11
RIVAL_WORKSPACE_ID = 12

# 两边的消息数与执行数互不相同，"把别人的也算进来了"在断言里一眼可见
ACME_MESSAGES = 3
RIVAL_MESSAGES = 5
ACME_EXECS = 2
RIVAL_EXECS = 4

ORPHAN_PRESET_ID = 4242


@pytest.fixture(params=["acme_row_first", "rival_row_first"])
async def db(request: pytest.FixtureRequest):
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed(rival_first=request.param == "rival_row_first")
        yield request.param
    finally:
        await Tortoise.close_connections()


async def seed(*, rival_first: bool) -> None:
    for tenant_id, name in ((ACME_TENANT_ID, "acme"), (RIVAL_TENANT_ID, "rival"), (BYSTANDER_TENANT_ID, "bystander")):
        await DBTenant.create(id=tenant_id, name=name, display_name=name)

    rows = [
        (ONEBOT, ACME_TENANT_ID, ACME_CHAT_KEY, ACME_WORKSPACE_ID, ACME_MESSAGES, ACME_EXECS),
        (WECHAT, RIVAL_TENANT_ID, RIVAL_CHAT_KEY, RIVAL_WORKSPACE_ID, RIVAL_MESSAGES, RIVAL_EXECS),
    ]
    if rival_first:
        rows.reverse()

    for adapter_key, tenant_id, chat_key, workspace_id, messages, execs in rows:
        await DBAdapterInstance.create(
            adapter_key=adapter_key,
            instance_key=SHARED_KEY,
            display_name=f"{adapter_key}-{tenant_id}",
            status="online",
            provider=adapter_key,
            tenant_id=tenant_id,
            metadata_json="",
        )
        await DBWorkspace.create(id=workspace_id, name=f"ws{workspace_id}", container_name=f"nekro-cc-{workspace_id}")
        await DBChatChannel.create(
            adapter_key=adapter_key,
            instance_key=SHARED_KEY,
            channel_id=chat_key,
            chat_key=chat_key,
            channel_type="group",
            workspace_id=workspace_id,
            data="{}",
        )
        for index in range(messages):
            await DBChatMessage.create(
                sender_id=f"{adapter_key}-sender-{index}",
                sender_name=f"{adapter_key}-name-{index}",
                sender_nickname=f"{adapter_key}-nick-{index}",
                is_tome=0,
                is_recalled=False,
                adapter_key=adapter_key,
                instance_key=SHARED_KEY,
                message_id=f"{adapter_key}-msg-{index}",
                chat_key=chat_key,
                chat_type=ChatType.GROUP,
                platform_userid=f"{adapter_key}-{index}",
                content_text="hi",
                content_data="[]",
                raw_cq_code="",
                ext_data="",
                send_timestamp=0,
            )
        for index in range(execs):
            await DBExecCode.create(
                chat_key=chat_key,
                trigger_user_id=f"{adapter_key}-user",
                trigger_user_name=f"{adapter_key}-name",
                success=True,
                code_text="print(1)",
                outputs="1",
                stop_type=ExecStopType.NORMAL,
                exec_time_ms=index,
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


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


# 每个租户自己那一份：(身份, 自己的适配器, 自己的频道, 对方的适配器, 对方的频道)
SIDES = [
    pytest.param(acme_admin, ONEBOT, ACME_CHAT_KEY, WECHAT, RIVAL_CHAT_KEY, id="acme"),
    pytest.param(rival_admin, WECHAT, RIVAL_CHAT_KEY, ONEBOT, ACME_CHAT_KEY, id="rival"),
]


class TestInstanceGuard:
    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_bare_key_resolves_to_the_callers_own_row(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        """只给标识时，租户拿到的是自己那一行，而不是行号更小的别人那一行（或被误判成 404）"""
        instance = await assert_instance_accessible(who(), SHARED_KEY)
        assert instance.adapter_key == own_adapter

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_adapter_key_pins_the_row(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        assert (await assert_instance_accessible(who(), SHARED_KEY, adapter_key=own_adapter)).adapter_key == own_adapter
        with pytest.raises(NotFoundError):
            await assert_instance_accessible(who(), SHARED_KEY, adapter_key=other_adapter)

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_accessible_refs_are_pairs(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        assert await accessible_instance_refs(who()) == {(own_adapter, SHARED_KEY)}

    async def test_platform_owner_is_unrestricted(self, db) -> None:  # noqa: ARG002
        """对照：平台方不受影响，带上适配器就能精确取到任一行"""
        for adapter_key in (ONEBOT, WECHAT):
            instance = await assert_instance_accessible(platform_owner(), SHARED_KEY, adapter_key=adapter_key)
            assert instance.adapter_key == adapter_key


class TestChannelScope:
    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_channel_list_and_guard(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.chat_channel import assert_channel_accessible, visible_channels

        visible = await (await visible_channels(who())).values_list("chat_key", flat=True)
        assert sorted(visible) == [own_chat]

        await assert_channel_accessible(who(), own_chat)
        with pytest.raises(NotFoundError):
            await assert_channel_accessible(who(), other_chat)

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_channel_list_sse_drops_the_other_adapters_same_named_channel(
        self, db, who, own_adapter, own_chat, other_adapter, other_chat, monkeypatch,
    ) -> None:  # noqa: ARG002
        """广播是全局的：对方同名实例的频道事件不能从 SSE 流里漏给本租户"""
        from nekro_agent.routers import chat_channel as chat_module
        from nekro_agent.services.channel_broadcaster import channel_broadcaster

        captured: Dict[str, Any] = {}

        class _CapturedResponse:
            def __init__(self, generator: Any, **_kwargs: Any) -> None:
                captured["generator"] = generator

        class _Request:
            async def is_disconnected(self) -> bool:
                return False

        monkeypatch.setattr(chat_module, "EventSourceResponse", _CapturedResponse)

        await chat_module.stream_chat_channel_list(
            request=_Request(),  # type: ignore[arg-type]
            _current_user=who(),
        )
        generator = captured["generator"]
        try:
            await channel_broadcaster.publish_update("updated", other_chat)
            await channel_broadcaster.publish_update("updated", own_chat)
            item = await generator.__anext__()
        finally:
            await generator.aclose()

        assert json.loads(item["data"])["chat_key"] == own_chat

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_timers_follow_the_channel(self, db, who, own_adapter, own_chat, other_adapter, other_chat, monkeypatch) -> None:  # noqa: ARG002
        from nekro_agent.routers import timers

        async def all_items() -> List[Any]:
            return [SimpleNamespace(chat_key=ACME_CHAT_KEY), SimpleNamespace(chat_key=RIVAL_CHAT_KEY)]

        monkeypatch.setattr(timers, "_build_all_items", all_items)
        assert [item.chat_key for item in await timers._accessible_items(who())] == [own_chat]


class _ConfigReached(Exception):
    """配置服务被调用了——说明归属守卫已经放行"""


class TestConfigScope:
    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    @pytest.mark.parametrize("for_write", [True, False])
    async def test_instance_override_key(self, db, who, own_adapter, own_chat, other_adapter, other_chat, for_write: bool) -> None:  # noqa: ARG002
        from nekro_agent.routers.config import _assert_config_key_accessible
        from nekro_agent.services.config_service import instance_override_config_key

        await _assert_config_key_accessible(who(), instance_override_config_key(own_adapter, SHARED_KEY), for_write=for_write)
        with pytest.raises(NotFoundError):
            await _assert_config_key_accessible(
                who(),
                instance_override_config_key(other_adapter, SHARED_KEY),
                for_write=for_write,
            )

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_channel_override_key(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.config import _assert_chat_key_accessible

        await _assert_chat_key_accessible(who(), own_chat)
        with pytest.raises(NotFoundError):
            await _assert_chat_key_accessible(who(), other_chat)

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_instance_config_endpoints(self, db, who, own_adapter, own_chat, other_adapter, other_chat, monkeypatch) -> None:  # noqa: ARG002
        """读写两个端点都在碰配置服务之前挡住别人的同名实例"""
        from nekro_agent.routers import config as config_router

        assert other_adapter in ADAPTER_REGISTRY, "适配器不在注册表里时端点在守卫之前就 404，用例会恒绿"

        def reached(*_args: Any, **_kwargs: Any) -> Any:
            raise _ConfigReached

        monkeypatch.setattr(config_router.UnifiedConfigService, "get_config_list", reached)
        monkeypatch.setattr(config_router.UnifiedConfigService, "batch_update_config", reached)

        with pytest.raises(NotFoundError):
            await config_router.get_instance_config(adapter_key=other_adapter, instance_key=SHARED_KEY, _current_user=who())
        with pytest.raises(NotFoundError):
            await config_router.update_instance_config(
                adapter_key=other_adapter,
                instance_key=SHARED_KEY,
                body=config_router.BatchUpdateConfig(configs={}),
                _current_user=who(),
            )
        # 对照：自己那一行放行（走到配置服务）
        with pytest.raises(_ConfigReached):
            await config_router.get_instance_config(adapter_key=own_adapter, instance_key=SHARED_KEY, _current_user=who())

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_config_key_listing(self, db, who, own_adapter, own_chat, other_adapter, other_chat, monkeypatch) -> None:  # noqa: ARG002
        from nekro_agent.routers import config as config_router
        from nekro_agent.services.config_service import CHANNEL_OVERRIDE_KEY_PREFIX, instance_override_config_key

        keys = [
            instance_override_config_key(ONEBOT, SHARED_KEY),
            instance_override_config_key(WECHAT, SHARED_KEY),
            f"{CHANNEL_OVERRIDE_KEY_PREFIX}{ACME_CHAT_KEY}",
            f"{CHANNEL_OVERRIDE_KEY_PREFIX}{RIVAL_CHAT_KEY}",
        ]
        monkeypatch.setattr(config_router.UnifiedConfigService, "get_all_config_keys", lambda: list(keys))

        visible = await config_router.get_config_keys(_current_user=who())
        assert sorted(visible) == sorted(
            [instance_override_config_key(own_adapter, SHARED_KEY), f"{CHANNEL_OVERRIDE_KEY_PREFIX}{own_chat}"],
        )


class TestAnnouncementScope:
    """公告是唯一能往群里发言的批量写入面，判错一行就是往别人的群里发消息"""

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_instances_scope_is_checked_per_pair(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.chat_channel import _scope_within_reach
        from nekro_agent.services.announcement import AnnouncementInstanceRef, AnnouncementScope

        def scope_of(adapter_key: str) -> AnnouncementScope:
            return AnnouncementScope(
                scope_type=AnnouncementScopeType.INSTANCES,
                instances=[AnnouncementInstanceRef(adapter_key=adapter_key, instance_key=SHARED_KEY)],
            )

        own = scope_of(own_adapter)
        assert await _scope_within_reach(who(), own) is own
        with pytest.raises(NotFoundError):
            await _scope_within_reach(who(), scope_of(other_adapter))

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_all_scope_and_options_keep_only_own_pair(self, db, who, own_adapter, own_chat, other_adapter, other_chat, monkeypatch) -> None:  # noqa: ARG002
        from nekro_agent.routers import chat_channel
        from nekro_agent.services.announcement import AnnouncementInstanceOption, AnnouncementScope

        async def both_options() -> List[AnnouncementInstanceOption]:
            return [
                AnnouncementInstanceOption(adapter_key=adapter_key, instance_key=SHARED_KEY, display_name=adapter_key, channel_count=1)
                for adapter_key in (ONEBOT, WECHAT)
            ]

        monkeypatch.setattr(chat_channel.announcement_service, "list_instance_options", both_options)

        narrowed = await chat_channel._scope_within_reach(who(), AnnouncementScope(scope_type=AnnouncementScopeType.ALL))
        assert [(ref.adapter_key, ref.instance_key) for ref in narrowed.instances] == [(own_adapter, SHARED_KEY)]

        options = (await chat_channel.list_announcement_instances(_current_user=who())).items
        assert [(option.adapter_key, option.instance_key) for option in options] == [(own_adapter, SHARED_KEY)]

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_history_is_attributed_per_pair(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.chat_channel import _accessible_announcements

        def announcement(scope_type: AnnouncementScopeType, value: Any) -> Dict[str, Any]:
            return {
                "content": "x",
                "scope_type": scope_type.value,
                "scope_value": json.dumps(value),
                "status": AnnouncementStatus.PENDING.value,
            }

        own_by_instance = await DBAnnouncement.create(
            **announcement(AnnouncementScopeType.INSTANCES, [{"adapter_key": own_adapter, "instance_key": SHARED_KEY}]),
        )
        other_by_instance = await DBAnnouncement.create(
            **announcement(AnnouncementScopeType.INSTANCES, [{"adapter_key": other_adapter, "instance_key": SHARED_KEY}]),
        )
        own_by_channel = await DBAnnouncement.create(**announcement(AnnouncementScopeType.CHANNELS, [own_chat]))
        other_by_channel = await DBAnnouncement.create(**announcement(AnnouncementScopeType.CHANNELS, [other_chat]))

        visible = await _accessible_announcements(
            who(),
            [own_by_instance, other_by_instance, own_by_channel, other_by_channel],
        )
        assert {item.id for item in visible} == {own_by_instance.id, own_by_channel.id}


class TestStatisticsAndEvents:
    @pytest.mark.parametrize(
        ("who", "messages", "execs"),
        [
            pytest.param(acme_admin, ACME_MESSAGES, ACME_EXECS, id="acme"),
            pytest.param(rival_admin, RIVAL_MESSAGES, RIVAL_EXECS, id="rival"),
        ],
    )
    async def test_dashboard_counts_only_own_pair(self, db, who, messages: int, execs: int) -> None:  # noqa: ARG002
        from nekro_agent.routers.dashboard import resolve_dashboard_scope

        scope = await resolve_dashboard_scope(who())
        assert await scope.messages().count() == messages
        assert await scope.exec_codes().count() == execs

    @pytest.mark.parametrize(
        ("who", "messages"),
        [pytest.param(acme_admin, ACME_MESSAGES, id="acme"), pytest.param(rival_admin, RIVAL_MESSAGES, id="rival")],
    )
    async def test_console_overview_counts_only_own_pair(self, db, who, messages: int) -> None:  # noqa: ARG002
        from nekro_agent.routers.console.overview import get_console_overview

        overview = await get_console_overview(_current_user=who())
        assert overview.instance_total == 1
        assert overview.channel_total == 1
        assert overview.message_count_today == messages

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_console_instance_lists(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.console.instances import get_console_instance_options, get_console_instances

        options = (await get_console_instance_options(_current_user=who())).items
        assert [(option.adapter_key, option.instance_key) for option in options] == [(own_adapter, SHARED_KEY)]
        items = (await get_console_instances(_current_user=who())).items
        assert [(item.adapter_key, item.instance_key) for item in items] == [(own_adapter, SHARED_KEY)]

    async def test_a_tenant_without_instances_sees_nothing(self, db) -> None:  # noqa: ARG002
        """空的实例集合必须编成"什么都不匹配"，而不是"不加条件"——后者等于全站可见"""
        from nekro_agent.routers.chat_channel import visible_channels
        from nekro_agent.routers.dashboard import resolve_dashboard_scope

        bystander = make_user(tenant_id=BYSTANDER_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)
        assert await (await visible_channels(bystander)).count() == 0
        scope = await resolve_dashboard_scope(bystander)
        assert await scope.messages().count() == 0
        assert await scope.exec_codes().count() == 0

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_event_stream_visibility(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.events import resolve_event_visibility

        visibility = await resolve_event_visibility(who())
        assert visibility is not None
        await visibility._refresh_if_stale()

        def status(adapter_key: str) -> Dict[str, Any]:
            return {"type": "adapter_instance_status", "adapter_key": adapter_key, "instance_key": SHARED_KEY}

        assert visibility._allows("adapter_instance_status", status(own_adapter))
        assert not visibility._allows("adapter_instance_status", status(other_adapter))
        assert visibility._allows("agent_active", {"chat_key": own_chat})
        assert not visibility._allows("agent_active", {"chat_key": other_chat})


class TestOwnershipDerivedFromAChannel:
    """频道 -> 实例 -> 租户 这条推导链上的几处，推错一行就是把频道判给另一个租户（或平台方）"""

    async def test_workspace_owner_groups(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.workspace.network_guard import workspace_group_key
        from nekro_agent.services.workspace.tenant_scope import resolve_workspace_owner_groups

        groups = await resolve_workspace_owner_groups([ACME_WORKSPACE_ID, RIVAL_WORKSPACE_ID])
        assert groups == {
            ACME_WORKSPACE_ID: workspace_group_key([ACME_TENANT_ID]),
            RIVAL_WORKSPACE_ID: workspace_group_key([RIVAL_TENANT_ID]),
        }

    async def test_workspace_network_group(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.workspace.network_guard import resolve_workspace_network_group, workspace_group_key

        for workspace_id, tenant_id in ((ACME_WORKSPACE_ID, ACME_TENANT_ID), (RIVAL_WORKSPACE_ID, RIVAL_TENANT_ID)):
            workspace = await DBWorkspace.get(id=workspace_id)
            assert await resolve_workspace_network_group(workspace) == workspace_group_key([tenant_id])

    @pytest.mark.parametrize(
        ("who", "workspace_id"),
        [pytest.param(acme_admin, ACME_WORKSPACE_ID, id="acme"), pytest.param(rival_admin, RIVAL_WORKSPACE_ID, id="rival")],
    )
    async def test_accessible_workspaces(self, db, who, workspace_id: int) -> None:  # noqa: ARG002
        from nekro_agent.services.workspace.tenant_scope import accessible_workspace_ids

        assert await accessible_workspace_ids(who()) == [workspace_id]

    async def test_skill_scope_of_a_channel(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services import skill_scope

        assert await skill_scope.channel_skill_scope(ACME_CHAT_KEY) == ACME_TENANT_ID
        assert await skill_scope.channel_skill_scope(RIVAL_CHAT_KEY) == RIVAL_TENANT_ID

    async def test_model_group_runtime_guard_resolves_the_channels_tenant(self, db, monkeypatch) -> None:  # noqa: ARG002
        from nekro_agent.services import model_group_service

        seen: List[Optional[int]] = []

        async def capture(_config: Any, *, tenant_id: int, scope_label: str) -> List[Tuple[str, str, str]]:  # noqa: ARG001
            seen.append(tenant_id)
            return []

        monkeypatch.setattr(model_group_service, "enforce_model_group_scope", capture)
        for adapter_key in (ONEBOT, WECHAT):
            await model_group_service.enforce_channel_model_group_scope(
                object(),  # type: ignore[arg-type]
                instance_key=SHARED_KEY,
                adapter_key=adapter_key,
                chat_key="x",
            )
        assert seen == [ACME_TENANT_ID, RIVAL_TENANT_ID]

    def test_run_agent_hands_the_channels_adapter_to_the_guard(self) -> None:
        """运行时兜底只有拿到频道的适配器才判得对；漏传就退回按标识反查"""
        from nekro_agent.services.agent import run_agent

        # run_agent 现在只解析频道并设置实例上下文，守卫调用在 _run_agent_in_instance_scope 里
        tree = ast.parse(inspect.getsource(run_agent._run_agent_in_instance_scope))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "enforce_channel_model_group_scope"
        ]
        assert calls, "run_agent 里没有模型组归属守卫"
        for call in calls:
            passed = {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}
            assert passed.get("adapter_key") == "db_chat_channel.adapter_key"

    async def test_quota_ceiling_is_read_from_the_written_row(self, db) -> None:  # noqa: ARG002
        """上限只设在 acme 那一行上：写 acme 的覆盖层必须被它挡住，写 rival 的不受它影响"""
        from nekro_agent.services.config_service import CHANNEL_OVERRIDE_KEY_PREFIX, instance_override_config_key
        from nekro_agent.services.quota_service import (
            QUOTA_LIMIT_FIELD,
            QUOTA_LIMIT_TOGGLE,
            assert_tenant_quota_within_ceiling,
            save_quota_ceiling,
        )

        await save_quota_ceiling(await DBAdapterInstance.get(adapter_key=ONEBOT, instance_key=SHARED_KEY), 10)
        over_ceiling = {QUOTA_LIMIT_TOGGLE: "true", QUOTA_LIMIT_FIELD: "999"}

        for acme_key in (instance_override_config_key(ONEBOT, SHARED_KEY), f"{CHANNEL_OVERRIDE_KEY_PREFIX}{ACME_CHAT_KEY}"):
            with pytest.raises(PermissionDeniedError):
                await assert_tenant_quota_within_ceiling(acme_key, over_ceiling)
        for rival_key in (instance_override_config_key(WECHAT, SHARED_KEY), f"{CHANNEL_OVERRIDE_KEY_PREFIX}{RIVAL_CHAT_KEY}"):
            await assert_tenant_quota_within_ceiling(rival_key, over_ceiling)


async def seed_affection(adapter_key: str, *, platform_userid: str, preset_id: int) -> None:
    await DBAffection.create(
        **ScopeDomain.of_instance(adapter_key, SHARED_KEY).as_columns(),
        platform_userid=platform_userid,
        preset_id=preset_id,
        nickname=f"nick-{platform_userid}",
        score=10,
        stage="stranger",
    )


class TestInstanceKeyedConsolePages:
    """人设与好感这类后台页只收 `instance_key` 一个参数，取到的实例行必须是调用方自己那一行"""

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_preset_page_writes_the_callers_row(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        """实例默认人设写在实例元数据里，取错行就是改了别人的实例"""
        from nekro_agent.routers.presets import _require_instance

        assert (await _require_instance(who(), SHARED_KEY)).adapter_key == own_adapter

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_affection_page_reads_the_callers_domain(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        from nekro_agent.routers.affection import _scoped_query, resolve_instance_scope

        await seed_affection(ONEBOT, platform_userid="acme-person", preset_id=0)
        await seed_affection(WECHAT, platform_userid="rival-person", preset_id=0)

        adapter_key, scope = await resolve_instance_scope(who(), SHARED_KEY)
        assert (adapter_key, scope) == (own_adapter, SHARED_KEY)
        people = await (await _scoped_query(scope, adapter_key)).values_list("platform_userid", flat=True)
        assert people == ["acme-person" if own_adapter == ONEBOT else "rival-person"]

    @pytest.mark.parametrize(("who", "own_adapter", "own_chat", "other_adapter", "other_chat"), SIDES)
    async def test_orphan_buckets_stay_in_the_callers_domain(self, db, who, own_adapter, own_chat, other_adapter, other_chat) -> None:  # noqa: ARG002
        """孤儿清单同时是清理接口的授权依据：列进别人的桶就能把别人的数据删掉"""
        from nekro_agent.routers.console.identity import get_identity_orphans

        await seed_affection(ONEBOT, platform_userid="acme-person", preset_id=ORPHAN_PRESET_ID)
        await seed_affection(WECHAT, platform_userid="rival-person", preset_id=ORPHAN_PRESET_ID)

        orphans = await get_identity_orphans(instance_key=SHARED_KEY, _current_user=who())
        assert [item.domain_id for item in orphans.items] == [ScopeDomain.of_instance(own_adapter, SHARED_KEY).domain_id]


class TestTenantBinding:
    async def test_binding_a_shared_key_never_takes_another_tenants_row(self, db) -> None:  # noqa: ARG002
        """按标识划归会落到全部同名行上，其中只要有一行已归属别的租户就必须拒绝

        改前按标识把同名行折叠成一行再判冲突：留下的恰好是未归属的那一行时放行，
        整批更新却把 acme 的实例一并改判给了别人。
        """
        from nekro_agent.routers.console.tenants import bind_tenant_instances
        from nekro_agent.schemas.tenant import TenantInstanceBindRequest

        await DBAdapterInstance.filter(adapter_key=WECHAT, instance_key=SHARED_KEY).update(tenant_id=None)

        with pytest.raises(ValidationError):
            await bind_tenant_instances(
                tenant_id=BYSTANDER_TENANT_ID,
                body=TenantInstanceBindRequest(instance_keys=[SHARED_KEY]),
                _current_user=platform_owner(),
            )
        acme_row = await DBAdapterInstance.get(adapter_key=ONEBOT, instance_key=SHARED_KEY)
        assert acme_row.tenant_id == ACME_TENANT_ID


class TestCreationGuard:
    """只按 `instance_key` 存取的数据（人设、插件开关、好感与记忆、协议端容器名）没法靠守卫分开，
    所以新的跨适配器重名在创建时就拒绝，与总后台的全表重名校验同一条判据"""

    async def test_adapter_registration_refuses_a_name_taken_by_another_adapter(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.adapter_instance_service import adapter_instance_service

        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="solo", provider=ONEBOT)
        with pytest.raises(ConflictError):
            await adapter_instance_service.create_instance(
                adapter_key=WECHAT,
                instance_key="solo",
                display_name="x",
                provider="ilink",
            )
        assert await DBAdapterInstance.filter(instance_key="solo").count() == 1

    async def test_adapter_registration_still_creates_and_stays_idempotent(self, db) -> None:  # noqa: ARG002
        """对照：新标识照常创建，同一适配器下重复登记照常幂等返回"""
        from nekro_agent.services.adapter_instance_service import adapter_instance_service

        created = await adapter_instance_service.create_instance(
            adapter_key=WECHAT,
            instance_key="fresh",
            display_name="x",
            provider="ilink",
        )
        again = await adapter_instance_service.create_instance(
            adapter_key=WECHAT,
            instance_key="fresh",
            display_name="y",
            provider="ilink",
        )
        assert again.id == created.id

    async def test_onebot_auto_registration_steps_over_a_name_taken_by_another_adapter(self, db) -> None:  # noqa: ARG002
        from nekro_agent.adapters.onebot_v11.core.instance import _register_unclaimed_instance

        await DBAdapterInstance.create(adapter_key=WECHAT, instance_key="ob123456", provider="ilink")
        registered = await _register_unclaimed_instance("123456")
        assert (registered.adapter_key, registered.instance_key) == (ONEBOT, "ob123456x")
        assert registered.provider_account_id == "123456"
