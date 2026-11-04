"""总后台「更换接入令牌」

实例自己的接入令牌此前只在建实例时生成，之后没有任何入口能换：从租户收回过的平台方实例
（令牌指纹进了「曾对租户可见」集合）在换令牌之前超级用户命令一直不生效，令牌泄露之后也
没法作废它。这里钉住四件事：

1. **新令牌真的生效**：元数据里换成了新值、`resolve_access_token` 返回新值、旧值不再被接受；
2. **旧连接被断开**：凭旧令牌握手的连接被 #108 的复核断开，凭新令牌重连照常按实例分发；
3. **送达按形态走，失败不回滚**：内置 NapCat 重启 / 只写配置 / qbob 重新接线 / 远程人工，
   送达抛异常时令牌仍是新的、原因进 `delivery_error`；
4. **可信来源自动恢复（#98）**：曝光过的旧令牌握手的连接判不可信，换完令牌凭新令牌握手的
   连接判可信。

日志、事件与异常文案里绝不出现令牌明文，这一点在事件与响应两处都有断言。
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from test_onebot_binding_takeover import Client, Stack, open_client
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import instances as instances_router
from nekro_agent.schemas.console import ContainerInfo, ContainerState, InstanceAccessTokenRotateResponse
from nekro_agent.schemas.errors import NotFoundError, OperationFailedError, ValidationError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import unclaimed_dispatch
from nekro_agent.services.console import container_manager, instance_manager, qbob_manager
from nekro_agent.services.user import token_exposure
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.super_users import is_trusted_super_user_source

ONEBOT = "onebot_v11"
DEPLOYMENT_TOKEN = "deployment-token"
OLD_TOKEN = "old-instance-token"
ACCOUNT = "40001"
TENANT_ID = 7


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.adapters.onebot_v11.core.instance import clear_instance_cache
    from nekro_agent.core.os_env import OsEnv

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    token_exposure._write([], seeded=True)  # noqa: SLF001
    monkeypatch.setattr(token_exposure, "_rebuild_pending", False)
    monkeypatch.setattr(token_exposure, "_rebuild_lock", None)
    monkeypatch.setattr(token_exposure, "_unwritten_fingerprints", set())
    monkeypatch.setattr(unclaimed_dispatch, "_last_warned", {})
    gate._presented_by_unbound_account.clear()  # noqa: SLF001
    clear_instance_cache()
    yield
    clear_instance_cache()


def set_deployment_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    if token:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", token)
    else:
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=TENANT_ID, name="acme", display_name="acme")
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def clients():
    """与复现用例同一份连接清单：用例结束时把还挂着的连接收掉"""
    opened: List[Client] = []
    yield opened
    for client in opened:
        await client.hang_up()


async def add_instance(
    instance_key: str,
    *,
    adapter_key: str = ONEBOT,
    provider: str = "remote_onebot",
    status: str = "offline",
    token: str = "",
    account: str = "",
    qbob_appid: str = "",
) -> DBAdapterInstance:
    metadata: Dict[str, Any] = {}
    if token:
        metadata["access_token"] = token
    if qbob_appid:
        metadata["qbob_appid"] = qbob_appid
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        provider=provider,
        provider_account_id=account,
        status=status,
        enabled=True,
        metadata_json=json.dumps(metadata) if metadata else "",
    )


async def fresh(instance: DBAdapterInstance) -> DBAdapterInstance:
    row = await DBAdapterInstance.get(id=instance.id)
    assert row is not None
    return row


def metadata_token(instance: DBAdapterInstance) -> str:
    return str(json.loads(instance.metadata_json or "{}").get("access_token") or "")


async def events_of(instance: DBAdapterInstance) -> List[DBAdapterInstanceEvent]:
    return await DBAdapterInstanceEvent.filter(instance_id=instance.id).order_by("id")


# ---------------------------------------------------------------------------
# 基本语义：新令牌生效、送达结果、事件不写令牌
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestRemoteInstance:
    async def test_remote_instance_gets_a_new_token_and_manual_delivery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """远程协议端：元数据换成新令牌、`resolve_access_token` 跟着变，送达结果是人工"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("remote", token=OLD_TOKEN)
        assert container_manager.resolve_access_token(instance) == OLD_TOKEN

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        row = await fresh(instance)
        assert metadata_token(row) == rotation.token
        assert rotation.token != OLD_TOKEN
        assert container_manager.resolve_access_token(row) == rotation.token
        assert rotation.delivery == "manual"
        assert rotation.delivery_error == ""
        assert rotation.disconnected == 0

    async def test_event_never_contains_the_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """事件说明里写操作人、断开条数与送达结果，但新旧令牌一个都不写"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("remote", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        events = await events_of(instance)
        assert [event.event_type for event in events] == ["access_token_rotated"]
        message = events[0].message
        assert "root" in message
        assert "manual" in message
        assert rotation.token not in message
        assert OLD_TOKEN not in message
        assert events[0].status_from == events[0].status_to == instance.status

    async def test_disabled_instance_can_rotate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """停用的实例可以换（收回之后、重新启用之前正是要换的时候）"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("parked", status="disabled", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert metadata_token(await fresh(instance)) == rotation.token
        assert rotation.delivery == "manual"


@pytest.mark.usefixtures("db")
class TestValidation:
    async def test_unclaimed_instance_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未认领实例的协议端凭部署级令牌连着，凭空给它一枚令牌会让它下一次握手就被拒"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("anon", status="unclaimed")

        with pytest.raises(ValidationError) as caught:
            await instance_manager.rotate_access_token(instance, operator="root")

        assert "请先认领" in caught.value.get_message(SupportedLang.ZH_CN)
        assert metadata_token(await fresh(instance)) == ""

    async def test_non_onebot_adapter_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("mailbox", adapter_key="email", provider="native")

        with pytest.raises(ValidationError) as caught:
            await instance_manager.rotate_access_token(instance, operator="root")

        assert "OneBot V11" in caught.value.get_message(SupportedLang.ZH_CN)


# ---------------------------------------------------------------------------
# 内置 NapCat：重启 / 只写配置 / 失败不回滚
# ---------------------------------------------------------------------------


@dataclass
class ContainerCalls:
    """跑用例的机器上没有 Docker：重启与重新下发都换成记录器，容器状态由用例指定（默认在跑）"""

    restarted: List[str]
    resynced: List[str]
    state: ContainerState = ContainerState.RUNNING


@pytest.fixture
def container_calls(monkeypatch: pytest.MonkeyPatch) -> ContainerCalls:
    calls = ContainerCalls(restarted=[], resynced=[])

    async def restart(instance: DBAdapterInstance, timeout: int = 15) -> None:
        calls.restarted.append(instance.instance_key)

    async def resync(instance: DBAdapterInstance) -> None:
        calls.resynced.append(instance.instance_key)

    async def info(instance: DBAdapterInstance) -> ContainerInfo:
        return ContainerInfo(name=f"nekro_napcat_{instance.instance_key}", state=calls.state)

    monkeypatch.setattr(container_manager, "restart_container", restart)
    monkeypatch.setattr(container_manager, "resync_network_config", resync)
    monkeypatch.setattr(container_manager, "get_container_info", info)
    return calls


@pytest.mark.usefixtures("db")
class TestBuiltinNapCat:
    async def test_existing_container_is_restarted(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls,
    ) -> None:
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "restarted"
        assert container_calls.restarted == ["napcat"]
        assert container_calls.resynced == []

    async def test_missing_container_only_writes_the_config(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls,
    ) -> None:
        """容器不存在：只重新下发配置，下次建容器时用的就是新令牌"""

        async def restart(instance: DBAdapterInstance, timeout: int = 15) -> None:
            raise NotFoundError(resource=f"容器 '{instance.instance_key}'")

        monkeypatch.setattr(container_manager, "restart_container", restart)
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "config_written"
        assert container_calls.resynced == ["napcat"]
        assert rotation.delivery_error == ""
        assert metadata_token(await fresh(instance)) == rotation.token

    async def test_delivery_failure_keeps_the_new_token(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls,
    ) -> None:
        """送达失败不回滚：旧令牌此刻已经不认了，回滚只会把一枚要作废的令牌救回来"""

        async def restart(instance: DBAdapterInstance, timeout: int = 15) -> None:
            raise OperationFailedError(operation="重启容器", detail="docker daemon 不可用")

        monkeypatch.setattr(container_manager, "restart_container", restart)
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "failed"
        assert rotation.delivery_error
        assert "重启容器" in rotation.delivery_error
        row = await fresh(instance)
        assert metadata_token(row) == rotation.token
        assert container_manager.resolve_access_token(row) == rotation.token
        events = await events_of(instance)
        assert "failed" in events[0].message
        assert rotation.token not in events[0].message
        assert OLD_TOKEN not in events[0].message

    async def test_delivery_error_text_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls,
    ) -> None:
        """第三方异常文本万一带出令牌，响应 / 事件 / 日志里只留掩码"""

        async def restart(instance: DBAdapterInstance, timeout: int = 15) -> None:
            raise RuntimeError(f"docker refused: Authorization=Bearer {metadata_token(instance)}")

        monkeypatch.setattr(container_manager, "restart_container", restart)
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "failed"
        assert rotation.token not in rotation.delivery_error
        assert OLD_TOKEN not in rotation.delivery_error
        assert "***" in rotation.delivery_error
        events = await events_of(instance)
        assert rotation.token not in events[0].message

    async def test_the_deployment_token_is_redacted_too(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls,
    ) -> None:
        """实例原本没有自己的令牌、靠部署级令牌认证：换之前生效的就是部署级令牌，它同样不能出现在报错里"""

        async def restart(instance: DBAdapterInstance, timeout: int = 15) -> None:
            raise RuntimeError(f"docker refused: Authorization=Bearer {DEPLOYMENT_TOKEN}")

        monkeypatch.setattr(container_manager, "restart_container", restart)
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat")

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "failed"
        assert DEPLOYMENT_TOKEN not in rotation.delivery_error
        assert "***" in rotation.delivery_error

    @pytest.mark.parametrize("status", ["disabled", "archived"])
    async def test_a_parked_instance_only_gets_the_config(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls, status: str,
    ) -> None:
        """停用 / 归档的实例：只把配置写好，不替管理员把容器拉起来（Docker 的 restart 会启动已停止的容器）"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN, status=status)
        await DBAdapterInstance.filter(id=instance.id).update(enabled=False)

        rotation = await instance_manager.rotate_access_token(await fresh(instance), operator="root")

        assert rotation.delivery == "config_written"
        assert container_calls.restarted == []
        assert container_calls.resynced == ["napcat"]

    @pytest.mark.parametrize("state", [ContainerState.EXITED, ContainerState.CREATED, ContainerState.PAUSED])
    async def test_a_stopped_container_is_not_started(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls, state: ContainerState,
    ) -> None:
        container_calls.state = state
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "config_written"
        assert container_calls.restarted == []
        assert container_calls.resynced == ["napcat"]

    async def test_concurrent_rotations_leave_the_container_on_the_stored_token(
        self, monkeypatch: pytest.MonkeyPatch, container_calls: ContainerCalls,
    ) -> None:
        """两次更换交错：后一次先写完库，前一次再拿自己那枚去下发配置——库里与容器里就成了两枚不同的令牌"""
        pushed: List[str] = []

        async def restart(instance: DBAdapterInstance, timeout: int = 15) -> None:
            if not pushed and not container_calls.restarted:
                container_calls.restarted.append("first")
                # 第一次下发慢一拍，让第二次有机会插进来
                await asyncio.sleep(0.1)
            pushed.append(metadata_token(instance))

        monkeypatch.setattr(container_manager, "restart_container", restart)
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("napcat", provider="builtin_napcat", token=OLD_TOKEN)

        first, second = await asyncio.gather(
            instance_manager.rotate_access_token(await fresh(instance), operator="a"),
            instance_manager.rotate_access_token(await fresh(instance), operator="b"),
        )

        stored = metadata_token(await fresh(instance))
        # 排队之后两次更换一前一后：各自下发的就是各自返回的那枚，最后一次就是库里那枚。
        # 交错时要么前一次把作废的令牌推进容器，要么前一次的响应给出一枚别人已经换掉的令牌
        assert pushed == [first.token, second.token], "有一次更换下发的令牌与它返回给界面的不是同一枚"
        assert pushed[-1] == stored, "最后下发进容器的令牌不是库里那枚"
        assert first.token != second.token


# ---------------------------------------------------------------------------
# 租户能看到的那一条事件
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestTenantVisibleEvent:
    async def test_a_tenant_instance_event_names_no_platform_operator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """租户在自己实例的健康事件里看得到这一条：不写平台方操作人，也不写原始报错"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("tenant_remote", token=OLD_TOKEN)
        await DBAdapterInstance.filter(id=instance.id).update(tenant_id=TENANT_ID)

        await instance_manager.rotate_access_token(await fresh(instance), operator="platform-root")

        message = (await events_of(instance))[0].message
        assert "platform-root" not in message
        assert "平台方" in message
        assert "manual" in message


# ---------------------------------------------------------------------------
# qbob：重新接线 / 还没接入 / 失败
# ---------------------------------------------------------------------------


@pytest.fixture
def qbob_calls(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    calls: List[str] = []

    async def rewire(instance: DBAdapterInstance) -> Any:
        calls.append(instance.instance_key)
        return None

    monkeypatch.setattr(qbob_manager, "rewire", rewire)
    return calls


@pytest.mark.usefixtures("db")
class TestBuiltinQbob:
    async def test_attached_bot_is_rewired(self, monkeypatch: pytest.MonkeyPatch, qbob_calls: List[str]) -> None:
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance(
            "qb", provider="builtin_qbob", token=OLD_TOKEN, qbob_appid="app-1",
        )

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "rewired"
        assert qbob_calls == ["qb"]

    async def test_no_bot_attached_is_reported_as_such(
        self, monkeypatch: pytest.MonkeyPatch, qbob_calls: List[str],
    ) -> None:
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("qb", provider="builtin_qbob", token=OLD_TOKEN)

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "not_attached"
        assert qbob_calls == []

    async def test_rewire_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def rewire(instance: DBAdapterInstance) -> Any:
            raise RuntimeError("qbob 不可达")

        monkeypatch.setattr(qbob_manager, "rewire", rewire)
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance(
            "qb", provider="builtin_qbob", token=OLD_TOKEN, qbob_appid="app-1",
        )

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.delivery == "failed"
        assert rotation.delivery_error == "qbob 不可达"
        assert metadata_token(await fresh(instance)) == rotation.token


# ---------------------------------------------------------------------------
# 在线连接：旧令牌握手的断开、新令牌重连照常分发、旧令牌重连被拒
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestLiveConnections:
    async def test_old_connection_is_dropped_and_new_token_works(
        self, monkeypatch: pytest.MonkeyPatch, clients: List[Client],
    ) -> None:
        """平台实例绑好账号、协议端凭旧令牌连上 -> 换令牌 -> 旧连接断开、新令牌重连照常分发"""
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("platform", token=OLD_TOKEN, account=ACCOUNT)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        old = open_client(clients, stack, OLD_TOKEN)
        assert await old.connect(), f"前提：旧令牌能握手成功（{old.close_reason}）"
        await old.group_message(1001)
        assert len(stack.dispatched_as("platform", 1001)) == 1, "前提：旧连接上的消息按该实例分发"

        rotation = await instance_manager.rotate_access_token(instance, operator="root")

        assert rotation.disconnected == 1
        assert await old.wait_closed(), "凭旧令牌握手的连接在换令牌之后仍然挂着"

        renewed = open_client(clients, stack, rotation.token)
        assert await renewed.connect(), f"凭新令牌重连被拒：{renewed.close_reason}"
        await renewed.group_message(1002)
        assert len(stack.dispatched_as("platform", 1002)) == 1, "凭新令牌的连接上的消息没有按该实例分发"

    async def test_old_token_cannot_reconnect(self, monkeypatch: pytest.MonkeyPatch, clients: List[Client]) -> None:
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("platform", token=OLD_TOKEN, account=ACCOUNT)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        await instance_manager.rotate_access_token(instance, operator="root")

        rejected = open_client(clients, stack, OLD_TOKEN)
        assert not await rejected.connect(), "旧令牌在换令牌之后还能握手成功"
        assert rejected.accepted.is_set() is False


# ---------------------------------------------------------------------------
# 可信来源恢复（#98）
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestTrustedSourceRecovery:
    """判定入口：`super_users.is_trusted_super_user_source`

    握手记录由真的握手中间件写进上下文，事件由 NoneBot 用 `create_task` 派发（复制当时的上下文），
    所以直接在 `Bot.handle_event` 这一层调判定，等价于「这条连接上派发的一条消息」。
    """

    async def test_exposed_token_is_untrusted_and_rotation_restores_trust(
        self, monkeypatch: pytest.MonkeyPatch, clients: List[Client],
    ) -> None:
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        instance = await add_instance("platform", token=OLD_TOKEN, account=ACCOUNT)
        token_exposure.record_exposed_tokens([OLD_TOKEN], reason="用例")
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        verdicts: List[bool] = []

        async def record_and_judge(bot: OneBotBot, event: Any) -> None:
            verdicts.append(await is_trusted_super_user_source(ONEBOT, "platform"))

        # `Stack` 已经把 `handle_event` 换成记录器；这里换成「判可信来源」的版本，
        # 派发那一刻的上下文就是那条连接的握手记录
        monkeypatch.setattr(OneBotBot, "handle_event", record_and_judge)

        old = open_client(clients, stack, OLD_TOKEN)
        assert await old.connect()
        await old.group_message(1001)
        assert verdicts == [False], "曝光过的令牌握手的连接被判成了可信来源"

        rotation = await instance_manager.rotate_access_token(instance, operator="root")
        assert await old.wait_closed()

        verdicts.clear()
        renewed = open_client(clients, stack, rotation.token)
        assert await renewed.connect(), f"凭新令牌重连被拒：{renewed.close_reason}"
        await renewed.group_message(1002)
        assert verdicts == [True], "换完令牌、凭新令牌握手的连接仍被判成不可信"


# ---------------------------------------------------------------------------
# 路由：平台方 Super 调得通，平台方 Admin 被拒
# ---------------------------------------------------------------------------


def _user(*, perm_level: Role) -> DBUser:
    return DBUser(
        username="root",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=perm_level,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(instances_router.router, prefix="/api/console")
    test_client = TestClient(app, raise_server_exceptions=False)

    def login_as(user: DBUser) -> None:
        app.dependency_overrides[get_current_active_user] = lambda: user

    test_client.login_as = login_as  # type: ignore[attr-defined]

    async def fake_rotate(instance: DBAdapterInstance, *, operator: str) -> Any:
        assert operator == "root"
        return instance_manager.AccessTokenRotation(
            token="brand-new-token", disconnected=2, delivery="manual", delivery_error="",
        )

    monkeypatch.setattr(instances_router, "rotate_access_token", fake_rotate)
    return test_client


@pytest.mark.usefixtures("db")
class TestRoute:
    async def test_platform_super_gets_the_new_token(self, client: TestClient) -> None:
        await add_instance("remote", token=OLD_TOKEN)
        client.login_as(_user(perm_level=Role.Super))  # type: ignore[attr-defined]

        response = client.post("/api/console/instances/remote/access-token/rotate")

        assert response.status_code == 200, response.text
        payload = InstanceAccessTokenRotateResponse.model_validate(response.json())
        assert payload.token == "brand-new-token"
        assert payload.disconnected == 2
        assert payload.delivery == "manual"
        assert payload.delivery_error == ""

    async def test_platform_admin_is_refused(self, client: TestClient) -> None:
        """`@require_role(Role.Super)` 真的挂上了：Admin 走真实 HTTP 栈拿到 403"""
        await add_instance("remote", token=OLD_TOKEN)
        client.login_as(_user(perm_level=Role.Admin))  # type: ignore[attr-defined]

        response = client.post("/api/console/instances/remote/access-token/rotate")

        assert response.status_code == 403, response.text

    async def test_unknown_instance_is_404(self, client: TestClient) -> None:
        client.login_as(_user(perm_level=Role.Super))  # type: ignore[attr-defined]

        response = client.post("/api/console/instances/nope/access-token/rotate")

        assert response.status_code == 404, response.text


def test_rotate_endpoint_is_platform_only_by_structure() -> None:
    """端点结构：`@require_role(Role.Super)` 且参数名叫 `_current_user`（名字不对装饰器不生效）"""
    import inspect

    from nekro_agent.services.user.perm import require_role

    endpoint = instances_router.rotate_console_instance_access_token
    assert inspect.signature(inspect.unwrap(endpoint)).parameters["_current_user"]
    assert inspect.getclosurevars(endpoint).nonlocals.get("min_role") is Role.Super
    assert require_role is not None
    assert asyncio.iscoroutinefunction(endpoint)
