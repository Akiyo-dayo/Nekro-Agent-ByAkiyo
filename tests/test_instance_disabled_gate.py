"""实例停用闸门的回归测试

保护的是「后台把实例停用之后，这个 Bot 真的哑了」这条契约。它曾经完全不成立：
`set_instance_enabled` 只写 `enabled=False` + `status=disabled`，而入站链路唯一的闸门
（`collector.py`）判的是**租户**是否停用，没有实例判据。实测停用一个实例之后，
它照常收消息、照常跑完 Agent、照常把回复发出去 —— 「停用」只是后台的一个标记。

因此这里钉死四件事：

1. `is_instance_active` 的判据表：实例开关与租户开关是**与**关系，
   而 `unclaimed` / `offline` / `error` 这些**运行态**绝不能被当成停用；
2. 入站在建频道行与命令识别**之前**就被挡住（否则被停用的 Bot 还会应答 `/` 命令）；
3. Agent 调度被挡住（通知事件与定时任务绕开了 collector，只靠出站闸门会白烧额度）；
4. 出站是**显式失败**而不是静默成功（WebUI / 公告 / 运维告警都要能看到这次没发出去）。

外加一条反向契约：未启用多租户、实例启用时，三处闸门必须逐字放行。
"""

import json
from typing import Any, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.interface.collector import collect_message
from nekro_agent.adapters.interface.schemas.platform import (
    PlatformChannel,
    PlatformMessage,
    PlatformSendResponse,
    PlatformUser,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.schemas.errors import InstanceDisabledError
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.message_service import message_service

ADAPTER = "onebot_v11"
INSTANCE = "mx2"
CHANNEL_ID = "group_123456"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{CHANNEL_ID}"
TENANT_ID = 1


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def seed_instance(
    *,
    enabled: bool = True,
    status: str = "online",
    tenant_id: Optional[int] = None,
    instance_key: str = INSTANCE,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
        enabled=enabled,
        provider="remote_onebot",
        provider_account_id="1000000012",
        tenant_id=tenant_id,
    )


async def seed_channel(*, instance_key: str = INSTANCE, chat_key: str = CHAT_KEY) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=CHANNEL_ID,
        channel_type=ChatType.GROUP.value,
        channel_name="",
        chat_key=chat_key,
        is_active=True,
        observe_mode=False,
        data=json.dumps({}),
    )


class FakeAdapter:
    """只实现闸门链路会碰到的那几个成员

    真适配器要走注册表与配置装配，而本文件测的是「消息有没有走到适配器那一步」，
    用一个记账替身即可，且能精确断言"一次也没被调用"。
    """

    key: str = ADAPTER

    def __init__(self) -> None:
        self.sent: List[str] = []
        self.command_probes: List[str] = []

    def build_chat_key(self, channel_id: str, instance_key: str = "") -> str:
        return f"{self.key}-{instance_key}-{channel_id}" if instance_key else f"{self.key}-{channel_id}"

    def detect_command(self, text: str) -> Optional[Tuple[str, str]]:
        self.command_probes.append(text)
        return None

    async def try_handle_wait_input(self, **_kwargs: Any) -> bool:
        return False

    async def forward_message(self, request: Any) -> PlatformSendResponse:
        self.sent.append(request.chat_key)
        return PlatformSendResponse(success=True, message_id="1")


def make_inbound() -> Tuple[PlatformChannel, PlatformUser, PlatformMessage]:
    return (
        PlatformChannel(
            channel_id=CHANNEL_ID,
            channel_name="",
            channel_type=ChatType.GROUP,
            instance_key=INSTANCE,
        ),
        PlatformUser(platform_name="qq", user_id="10001", user_name="tester"),
        PlatformMessage(
            message_id="1",
            sender_id="10001",
            sender_name="tester",
            content_text="/help",
        ),
    )


class TestGatePredicate:
    """判据表：实例开关 ∧ 租户开关，运行态不参与"""

    @pytest.mark.parametrize(
        ("enabled", "status", "expected"),
        [
            (True, "online", True),
            (True, "offline", True),
            (True, "connecting", True),
            (True, "error", True),
            (True, "login_required", True),
            # 协议端先于登记连入是常态，未认领实例必须照常收发，否则新接入的 Bot 静默失灵
            (True, "unclaimed", True),
            (False, "disabled", False),
            (False, "archived", False),
            (False, "deleted", False),
            # 两列不一致的脏行：以界面上显示的「已停用」为准
            (True, "disabled", False),
            (True, "archived", False),
            (False, "online", False),
        ],
    )
    async def test_instance_switch(self, db, enabled: bool, status: str, expected: bool) -> None:  # noqa: ARG002
        await seed_instance(enabled=enabled, status=status)
        assert await DBAdapterInstance.is_instance_active(ADAPTER, INSTANCE) is expected

    @pytest.mark.parametrize(
        ("instance_enabled", "tenant_status", "expected"),
        [
            (True, "active", True),
            (True, "suspended", False),
            (True, "archived", False),
            # 叠加：任一侧关闭即停发，启用实例救不回一个被停用的租户
            (False, "active", False),
            (False, "suspended", False),
        ],
    )
    async def test_tenant_switch_is_anded(
        self,
        db,  # noqa: ARG002
        instance_enabled: bool,
        tenant_status: str,
        expected: bool,
    ) -> None:
        await DBTenant.create(id=TENANT_ID, name="acme_ltd", display_name="ACME", status=tenant_status)
        await seed_instance(
            enabled=instance_enabled,
            status="online" if instance_enabled else "disabled",
            tenant_id=TENANT_ID,
        )
        assert await DBAdapterInstance.is_instance_active(ADAPTER, INSTANCE) is expected

    async def test_unknown_instance_is_allowed(self, db) -> None:  # noqa: ARG002
        """未启用多租户的部署里有大量空实例段的历史频道，判成停用会整片停摆"""
        assert await DBAdapterInstance.is_instance_active(ADAPTER, "") is True
        assert await DBAdapterInstance.is_instance_active(ADAPTER, "never_registered") is True

    async def test_same_instance_key_under_two_adapters_is_not_confused(self, db) -> None:  # noqa: ARG002
        """`instance_key` 只在适配器内唯一，判据必须带 adapter_key

        `unique_together = (adapter_key, instance_key)`，而适配器自己的登记接口
        （`adapters/wechat_ilink_multi/routers.py`）只查这个二元组，不查全表重名。
        因此两个适配器下同名的实例是能共存的；只按 `instance_key` 查会随机命中一行，
        两个方向都错：停用一个把另一个闷掉，或者停用完全不生效。
        """
        await seed_instance(enabled=False, status="disabled")
        await DBAdapterInstance.create(
            adapter_key="wechat_ilink_multi",
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="native",
            provider_account_id="wx-account",
        )

        assert await DBAdapterInstance.is_instance_active(ADAPTER, INSTANCE) is False
        assert await DBAdapterInstance.is_instance_active("wechat_ilink_multi", INSTANCE) is True

    async def test_chat_key_lookup(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=False, status="disabled")
        await seed_channel()
        assert await DBAdapterInstance.is_chat_key_active(CHAT_KEY) is False
        # 频道行不存在时放行：下游会给出更准确的报错
        assert await DBAdapterInstance.is_chat_key_active(f"{ADAPTER}-mx9-group_1") is True


class TestInboundGate:
    """入站必须在建频道行与命令识别之前就断掉"""

    async def test_disabled_instance_drops_message_before_anything_happens(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=False, status="disabled")
        adapter = FakeAdapter()

        await collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert await DBChatChannel.all().count() == 0
        # `/help` 是命令形态：闸门若排在命令识别之后，被停用的 Bot 仍会应答
        assert adapter.command_probes == []

    async def test_enabled_instance_still_flows_through(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=True, status="online")
        adapter = FakeAdapter()

        await collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert await DBChatChannel.filter(chat_key=CHAT_KEY).count() == 1
        assert adapter.command_probes == ["/help"]

    async def test_suspended_tenant_drops_message(self, db) -> None:  # noqa: ARG002
        """租户维度的既有闸门在挪到最前面之后仍然成立"""
        await DBTenant.create(id=TENANT_ID, name="acme_ltd", display_name="ACME", status="suspended")
        await seed_instance(tenant_id=TENANT_ID)
        adapter = FakeAdapter()

        await collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert await DBChatChannel.all().count() == 0


class TestMessageServiceInboundGate:
    """通知事件绕开 collector 直接调进 message_service，这一层必须自己挡住

    `adapters/onebot_v11/matchers/notice.py`（戳一戳、进群退群、撤回）与
    `adapters/minecraft/matchers/notice.py` 都是直接调 `push_human_message` /
    `push_system_message` 的，一步也不经过 `collect_message`。

    只挡 `schedule_agent_task` 是不够的，因为 `push_human_message` **自己就会把消息发出去**：
    配额用尽分支直接调 `adapter.forward_message` 通报「今日回复配额已用完」，
    既不过入站闸门也不过出站闸门——被停用的实例会当着群里的面说话。
    同一段代码之前还会跑插件的 `on_user_message` 挂载点并把消息落库。

    放行方向由 `TestGatePredicate` 的判据表覆盖，这里只钉「被停用时一个字节也不许往下走」。
    """

    def _human_message(self) -> ChatMessage:
        return ChatMessage(
            message_id="",
            sender_id="10001",
            sender_name="tester",
            sender_nickname="tester",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid="10001",
            is_tome=1,
            is_recalled=False,
            chat_key=CHAT_KEY,
            chat_type=ChatType.GROUP,
            content_text="(戳了戳你)",
            content_data=[],
            raw_cq_code="",
            ext_data={},
            send_timestamp=0,
        )

    async def test_disabled_instance_drops_notice_event(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=False, status="disabled")
        await seed_channel()

        await message_service.push_human_message(message=self._human_message())

        assert await DBChatMessage.all().count() == 0

    async def test_disabled_instance_drops_system_message(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=False, status="disabled")
        await seed_channel()

        await message_service.push_system_message(chat_key=CHAT_KEY, agent_messages="定时任务到点了")

        assert await DBChatMessage.all().count() == 0

    async def test_suspended_tenant_drops_notice_event(self, db) -> None:  # noqa: ARG002
        await DBTenant.create(id=TENANT_ID, name="acme_ltd", display_name="ACME", status="suspended")
        await seed_instance(tenant_id=TENANT_ID)
        await seed_channel()

        await message_service.push_human_message(message=self._human_message())

        assert await DBChatMessage.all().count() == 0


class TestAgentScheduleGate:
    """通知事件与定时任务绕开 collector，必须在调度处再挡一次"""

    @pytest.fixture(autouse=True)
    def _quiet_debounce(self, monkeypatch) -> None:
        """把防抖任务换成空转，避免测试真的去跑一轮 Agent"""

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return

        monkeypatch.setattr(type(message_service), "_debounce_task", noop)
        message_service.pending_messages.pop(CHAT_KEY, None)
        message_service.debounce_timers.pop(CHAT_KEY, None)

    async def test_disabled_instance_is_not_scheduled(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=False, status="disabled")
        await seed_channel()

        await message_service.schedule_agent_task(chat_key=CHAT_KEY)

        assert CHAT_KEY not in message_service.pending_messages

    async def test_enabled_instance_is_scheduled(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=True, status="online")
        await seed_channel()

        await message_service.schedule_agent_task(chat_key=CHAT_KEY)

        assert CHAT_KEY in message_service.pending_messages


class TestOutboundGate:
    """出站显式失败，且一个字节也不能进适配器"""

    async def test_disabled_instance_refuses_send(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=False, status="disabled")
        await seed_channel()
        adapter = FakeAdapter()

        with pytest.raises(InstanceDisabledError):
            await universal_chat_service.send_agent_message(
                chat_key=CHAT_KEY,
                messages="MXDIS-OFF-mx2",
                adapter=adapter,  # type: ignore[arg-type]
            )

        assert adapter.sent == []

    async def test_suspended_tenant_refuses_send(self, db) -> None:  # noqa: ARG002
        await DBTenant.create(id=TENANT_ID, name="acme_ltd", display_name="ACME", status="suspended")
        await seed_instance(tenant_id=TENANT_ID)
        await seed_channel()
        adapter = FakeAdapter()

        with pytest.raises(InstanceDisabledError):
            await universal_chat_service.send_agent_message(
                chat_key=CHAT_KEY,
                messages="MXDIS-OFF-mx2",
                adapter=adapter,  # type: ignore[arg-type]
            )

        assert adapter.sent == []

    async def test_enabled_instance_sends(self, db) -> None:  # noqa: ARG002
        await seed_instance(enabled=True, status="online")
        await seed_channel()
        adapter = FakeAdapter()

        await universal_chat_service.send_agent_message(
            chat_key=CHAT_KEY,
            messages="MXDIS-ON-mx2",
            adapter=adapter,  # type: ignore[arg-type]
        )

        assert adapter.sent == [CHAT_KEY]


class TestReEnableIsHot:
    """停用/启用必须热生效：判据不带缓存，改完立刻换答案"""

    async def test_toggle_takes_effect_without_restart(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.console.instance_manager import set_instance_enabled

        instance = await seed_instance(enabled=True, status="online")
        await seed_channel()
        assert await DBAdapterInstance.is_chat_key_active(CHAT_KEY) is True

        instance = await set_instance_enabled(instance, enabled=False)
        assert instance.status == "disabled"
        assert await DBAdapterInstance.is_chat_key_active(CHAT_KEY) is False

        instance = await set_instance_enabled(instance, enabled=True)
        assert instance.status == "offline"
        assert await DBAdapterInstance.is_chat_key_active(CHAT_KEY) is True
