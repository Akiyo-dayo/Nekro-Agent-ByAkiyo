"""掉线监测在多实例下的回归测试（需求 R15）

改造前探活调的是 `adapter.get_self_info(chat_key=None)`，而探活是后台任务、没有入站链路
留下的实例 contextvar，于是落进无参 `get_bot()` 的「唯一在线账号」推定。服务器上两个方向
都实测到了：

- **多个账号在线**：推定失败，三条健康连接全被判成掉线，且因为探活永远不可能成功，
  这是每个周期都发一次的持续误报，不是抖动。
- **恰好一个账号在线**：那一条连接替**所有**实例回答"在线"，协议端根本不存在的实例
  也被判成在线 —— 「用 A 的连接回答了 B 的问题」，只是发生在监测面。

因此本文件钉住的契约是：**每个实例的健康状态只能由它自己那条连接决定**。用例走的是
真实链路（健康服务 → 真实 OneBot 适配器 → 真实 `get_bot`），只把「哪些账号连着」这一层
换成假连接，否则换个假适配器就只是在测试自己写的桩。
"""

from typing import Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter
from nekro_agent.adapters.onebot_v11.core import bot as bot_module
from nekro_agent.adapters.onebot_v11.core import instance as instance_module
from nekro_agent.adapters.wechat_ilink_multi.adapter import WeChatILinkMultiAdapter
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.console import InstanceStatus
from nekro_agent.services.console import instance_health as health_module
from nekro_agent.services.console.instance_health import InstanceHealthService

ADAPTER = "onebot_v11"
# 实例标识与账号号码刻意不同形，防止用例在「实例 == 账号」的巧合上通过
INSTANCES: Tuple[Tuple[str, str], ...] = (("mx1", "10001"), ("mx2", "10002"), ("mx3", "10003"))


class FakeBot:
    """假的 OneBot V11 连接，记录被调用过的动作"""

    def __init__(self, self_id: str, *, api_error: Optional[Exception] = None) -> None:
        self.self_id: str = self_id
        self.api_error: Optional[Exception] = api_error
        self.calls: List[str] = []

    async def call_api(self, api: str, **_kwargs: object) -> Dict[str, object]:
        self.calls.append(api)
        if self.api_error is not None:
            raise self.api_error
        return {"user_id": int(self.self_id), "nickname": "FakeBot"}


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


@pytest.fixture
def notifications(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str]]:
    """拦截告警投递，返回 (level, title) 列表

    告警现在按**被告警的那个实例**解析投递通道（B7），所以调度入口多了
    `adapter_key` / `instance_key` 两个关键字参数。替身必须收下它们：不收的话
    `_notify` 会把 TypeError 当成"投递失败"吞掉，本文件里每一条告警断言都会静默变成
    「一条都没发」——那是一次**测试替身**造成的假失败，不是产品行为变了。
    实例这一维本身的断言在 `tests/test_protocol_selfservice_tenant_scope.py`。
    """
    sent: List[Tuple[str, str]] = []

    async def _fake_dispatch(  # noqa: ARG001
        title: str,
        body: str,
        level: str = "info",
        *,
        adapter_key: str = "",
        instance_key: str = "",
    ) -> Dict[str, bool]:
        sent.append((level, title))
        return {"stub": True}

    monkeypatch.setattr(health_module, "dispatch_notification", _fake_dispatch)
    return sent


async def seed_instances(unbound: bool = False) -> None:
    for index, (instance_key, account_id) in enumerate(INSTANCES):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=f"矩阵实例{index + 1}",
            provider="onebot_v11",
            provider_account_id="" if unbound else account_id,
            status=InstanceStatus.OFFLINE.value,
        )


def wire_connections(monkeypatch: pytest.MonkeyPatch, bots: Dict[str, FakeBot]) -> None:
    """把「当前有哪些账号连着」换成假连接，其余链路保持真实

    `bots` 后续可以就地增删，`get_bots()` 每次读的都是最新内容。
    """
    monkeypatch.setattr(bot_module, "get_bots", lambda: dict(bots))
    monkeypatch.setattr(bot_module, "OneBotV11Bot", FakeBot)

    adapter = OnebotV11Adapter()
    monkeypatch.setattr("nekro_agent.adapters.get_adapter", lambda adapter_key: adapter)  # noqa: ARG005


async def probe_once(service: InstanceHealthService) -> Dict[str, str]:
    await service._probe_all()  # noqa: SLF001
    rows = await DBAdapterInstance.all().order_by("id")
    return {row.instance_key: row.status for row in rows}


class TestProbeLocatesOwnConnection:
    """三种拓扑下，每个实例的判定只能由它自己那条连接决定"""

    @pytest.mark.parametrize(
        ("online_accounts", "expected"),
        [
            pytest.param(
                ("10001", "10002", "10003"),
                {"mx1": "online", "mx2": "online", "mx3": "online"},
                id="三个账号全在线",
            ),
            pytest.param(
                ("10002",),
                {"mx1": "offline", "mx2": "online", "mx3": "offline"},
                id="只有一个账号在线",
            ),
            pytest.param(
                (),
                {"mx1": "offline", "mx2": "offline", "mx3": "offline"},
                id="全部离线",
            ),
        ],
    )
    async def test_topologies(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        online_accounts: Tuple[str, ...],
        expected: Dict[str, str],
    ) -> None:
        await seed_instances()
        wire_connections(monkeypatch, {account: FakeBot(account) for account in online_accounts})

        assert await probe_once(InstanceHealthService()) == expected

    async def test_probe_really_talks_to_the_protocol_side(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """每条连接都必须被单独问一次，而不是读内存里的 self_id

        `get_self_info()` 只返回 Bot 对象上的 `self_id`，不产生任何协议端交互，
        socket 还在、对端已死的假连接照样"成功"——而假连接正是主动探活存在的理由。
        """
        await seed_instances()
        bots = {account: FakeBot(account) for _key, account in INSTANCES}
        wire_connections(monkeypatch, bots)

        await probe_once(InstanceHealthService())

        assert {account: bot.calls for account, bot in bots.items()} == {
            "10001": ["get_login_info"],
            "10002": ["get_login_info"],
            "10003": ["get_login_info"],
        }

    async def test_wedged_protocol_side_is_offline(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """连接还在但协议端回不了话 → 该实例离线，其余实例不受连累"""
        await seed_instances()
        wire_connections(
            monkeypatch,
            {
                "10001": FakeBot("10001"),
                "10002": FakeBot("10002", api_error=RuntimeError("api timeout")),
                "10003": FakeBot("10003"),
            },
        )

        assert await probe_once(InstanceHealthService()) == {
            "mx1": "online",
            "mx2": "offline",
            "mx3": "online",
        }

    async def test_unbound_instance_never_borrows_a_connection(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没绑账号的实例不可能在线，哪怕此刻整个进程只有一条连接

        这正是"恰好一个账号在线"那条推定最危险的形态：一个从未登录过的实例
        会被另一个账号的连接判成在线。
        """
        await seed_instances(unbound=True)
        wire_connections(monkeypatch, {"10002": FakeBot("10002")})

        assert await probe_once(InstanceHealthService()) == {
            "mx1": "offline",
            "mx2": "offline",
            "mx3": "offline",
        }


class TestOfflineAlerting:
    """抖动抑制与告警去重：真掉线仍要告警，真在线不能误报"""

    async def test_no_alert_before_delay_elapses(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instances()
        wire_connections(monkeypatch, {})
        monkeypatch.setattr(health_module.config, "INSTANCE_OFFLINE_ALERT_DELAY_SECONDS", 3600)

        service = InstanceHealthService()
        await probe_once(service)
        await probe_once(service)

        assert notifications == []

    async def test_alerts_once_then_recovers_once(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """掉线只告警一次，恢复补一条恢复通知，两侧的计数器都被清干净"""
        await seed_instances()
        bots: Dict[str, FakeBot] = {}
        wire_connections(monkeypatch, bots)
        monkeypatch.setattr(health_module.config, "INSTANCE_OFFLINE_ALERT_DELAY_SECONDS", 0)

        service = InstanceHealthService()
        await probe_once(service)
        await probe_once(service)
        await probe_once(service)

        assert notifications == [
            ("warning", "实例掉线告警: 矩阵实例1"),
            ("warning", "实例掉线告警: 矩阵实例2"),
            ("warning", "实例掉线告警: 矩阵实例3"),
        ]
        snapshot = {item.instance_key: item for item in (await service.list_health()).items}
        assert snapshot["mx1"].consecutive_failures == 3
        assert snapshot["mx1"].alerted is True

        notifications.clear()
        bots["10001"] = FakeBot("10001")
        assert (await probe_once(service))["mx1"] == "online"

        assert notifications == [("info", "实例已恢复在线: 矩阵实例1")]
        recovered = {item.instance_key: item for item in (await service.list_health()).items}["mx1"]
        assert recovered.consecutive_failures == 0
        assert recovered.alerted is False
        assert recovered.offline_since is None

    async def test_quiet_hours_defer_instead_of_swallow(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """静默时段只跳过本次发送，窗口结束后仍要补发，而不是永久吞掉"""
        await seed_instances()
        wire_connections(monkeypatch, {})
        monkeypatch.setattr(health_module.config, "INSTANCE_OFFLINE_ALERT_DELAY_SECONDS", 0)
        # 静默时段现在按实例解析（B7），替身要收下那个实例参数
        monkeypatch.setattr(InstanceHealthService, "_in_quiet_hours", lambda _self, _now, _instance=None: True)

        service = InstanceHealthService()
        await probe_once(service)
        assert notifications == []
        assert {item.instance_key: item.alerted for item in (await service.list_health()).items} == {
            "mx1": False,
            "mx2": False,
            "mx3": False,
        }

        monkeypatch.setattr(InstanceHealthService, "_in_quiet_hours", lambda _self, _now, _instance=None: False)
        await probe_once(service)
        assert [title for _level, title in notifications] == [
            "实例掉线告警: 矩阵实例1",
            "实例掉线告警: 矩阵实例2",
            "实例掉线告警: 矩阵实例3",
        ]


class TestOnebotProbeResolution:
    """适配器侧的定位契约：绝不允许用别人的连接回答本实例的问题"""

    async def test_resolves_bound_account(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await seed_instances()
        wire_connections(monkeypatch, {account: FakeBot(account) for _key, account in INSTANCES})

        assert await OnebotV11Adapter().probe_instance("mx3") == "10003"

    async def test_offline_account_raises_instead_of_falling_back(
        self,
        db: None,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instances()
        wire_connections(monkeypatch, {"10002": FakeBot("10002")})

        with pytest.raises(bot_module.BotInstanceOfflineError) as excinfo:
            await OnebotV11Adapter().probe_instance("mx1")
        # 修好之后探活再也产生不了"未指定实例"那条文案：目标实例一直是明确的
        assert "未指定实例" not in str(excinfo.value)
        assert excinfo.value.target == "10001"

    async def test_unbound_instance_raises(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await seed_instances(unbound=True)
        wire_connections(monkeypatch, {"10002": FakeBot("10002")})

        with pytest.raises(bot_module.BotInstanceOfflineError) as excinfo:
            await OnebotV11Adapter().probe_instance("mx1")
        assert excinfo.value.target == "mx1"


class TestAdapterOwnedStatusesAreNotRewritten:
    """适配器自己的生命周期态不能被探活覆盖掉

    `binding` / `session_expired` / `renewing` 由 `wechat_ilink_multi` 的绑定与续期流程写入，
    与 `login_required` 同形：都带着比 online/offline 更精确的下一步动作。探活改写它们，
    管理员就只能看到一个"离线"（或更糟，一个假的"在线"），不知道该去扫码还是去重新绑定。
    """

    @pytest.mark.parametrize("adapter_owned_status", ["binding", "session_expired", "renewing"])
    async def test_probe_leaves_adapter_owned_status_alone(
        self,
        db: None,  # noqa: ARG002
        notifications: List[Tuple[str, str]],
        monkeypatch: pytest.MonkeyPatch,
        adapter_owned_status: str,
    ) -> None:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="mx9",
            display_name="绑定中的实例",
            provider="onebot_v11",
            provider_account_id="10009",
            status=adapter_owned_status,
        )
        # 一条连接都没有：不豁免的话这一轮必然把状态改写成 offline 并进入告警计时
        wire_connections(monkeypatch, {})
        monkeypatch.setattr(health_module.config, "INSTANCE_OFFLINE_ALERT_DELAY_SECONDS", 0)

        assert await probe_once(InstanceHealthService()) == {"mx9": adapter_owned_status}
        assert notifications == []


class TestConnectEventUsesTheSameScopeAsTheProbe:
    """连接事件与探活必须用同一套「谁参与监测」判据

    `_probe_all` 按 `enabled=True` + 状态过滤，`_report` 曾经只看状态。两套判据下，
    `enabled=False` 却仍是运行态的脏行（升级前的存量行、只改了一列的手工修补）会被
    协议端的一次重连改写成 online —— 总后台上一个"已停用"的实例显示成在线。
    """

    async def test_disabled_instance_is_not_brought_online_by_a_reconnect(
        self,
        db: None,  # noqa: ARG002
    ) -> None:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="mx8",
            display_name="脏行：已停用但状态仍是运行态",
            provider="onebot_v11",
            provider_account_id="10008",
            enabled=False,
            status=InstanceStatus.OFFLINE.value,
        )

        await InstanceHealthService().report_connect(ADAPTER, "mx8", message="协议端已连接: 10008")

        row = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key="mx8")
        assert row.status == InstanceStatus.OFFLINE.value

    async def test_enabled_instance_still_goes_online_on_connect(self, db: None) -> None:  # noqa: ARG002
        await seed_instances()

        await InstanceHealthService().report_connect(ADAPTER, "mx1", message="协议端已连接: 10001")

        row = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key="mx1")
        assert row.status == InstanceStatus.ONLINE.value


class TestMultiAccountAdapterMustLocateItsOwnConnection:
    """另一个多账号适配器（`wechat_ilink_multi`）必须覆写 `probe_instance`

    它的 `get_self_info()` 取 `online_keys[0]`，一条在线连接会替所有实例回答"在线"；
    一条都不在线时它返回 `user_id=""` 而不是抛异常，于是**默认实现**下这个适配器的实例恒为在线。
    """

    class FakeConnection:
        def __init__(self, *, online: bool, account_id: str) -> None:
            self.is_online: bool = online
            self.instance = DBAdapterInstance(instance_key="unused", provider_account_id=account_id)

    def _adapter_with(self, connections: Dict[str, "TestMultiAccountAdapterMustLocateItsOwnConnection.FakeConnection"]):
        adapter = WeChatILinkMultiAdapter()
        adapter.bot_manager._connections.update(connections)  # noqa: SLF001
        return adapter

    async def test_online_instance_reports_its_own_account(self) -> None:
        adapter = self._adapter_with({"wx1": self.FakeConnection(online=True, account_id="wxid_aaa")})

        assert await adapter.probe_instance("wx1") == "wxid_aaa"

    @pytest.mark.parametrize(
        ("connections", "target"),
        [
            pytest.param({}, "wx1", id="一条连接都没有"),
            pytest.param({"wx1": ("offline", "wxid_aaa")}, "wx1", id="连接在但不在线"),
            pytest.param({"wx2": ("online", "wxid_bbb")}, "wx1", id="别的实例在线也不能顶替"),
        ],
    )
    async def test_absent_or_offline_connection_raises(
        self,
        connections: Dict[str, Tuple[str, str]],
        target: str,
    ) -> None:
        adapter = self._adapter_with(
            {
                key: self.FakeConnection(online=state == "online", account_id=account)
                for key, (state, account) in connections.items()
            },
        )

        with pytest.raises(RuntimeError, match=target):
            await adapter.probe_instance(target)
