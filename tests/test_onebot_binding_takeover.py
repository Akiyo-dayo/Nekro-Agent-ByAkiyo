"""账号绑定到实例之后，握手对不上的旧连接不能继续顶着这个账号

## 场景

握手时账号还没绑定，中间件除了部署级令牌，还接受任何「等着绑账号」实例自己那枚令牌（备选令牌）。
租户手里有一个待绑实例 T 的令牌；平台方正要把某个账号绑到自己的实例 P 上。租户抢先带着这个账号连上来：

1. 连接一直挂着。账号绑到 P 之后它不会被断开；
2. 它发来的消息被当成 P 的消息分发（P 已认领，未认领闸门不拦）；
3. NoneBot 一个账号只留一条连接：P 真正的协议端因 `Duplicate X-Self-ID` 被拒，NA 发给这个账号的消息也都走到租户那条连接上。

部署级令牌为空时连备选令牌都不需要：账号未绑定时谁都能空令牌连上来。

## 这里怎么搭

用真的 NoneBot OneBot V11 适配器（`_handle_ws` 里的重复账号检查、`json_to_event`、`create_task` 派发）和真的握手中间件，
在用例自己的事件循环里直接驱动 ASGI，不起服务器。事件派发处把 `Bot.handle_event` 换成记录器，记录器做的正是
OneBot 消息匹配器的开头两步（`matchers/message.py`：`resolve_instance_key` → `is_inbound_dispatch_allowed`），
记下这条事件最终被当成哪个实例、会不会分发。
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import adapter as onebot_adapter_module
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.config import Config as NoneBotConfig
from nonebot.config import Env
from nonebot.drivers.fastapi import Driver as FastAPIDriver
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.adapters.onebot_v11.core.access_token import OneBotAccessTokenMiddleware
from nekro_agent.adapters.onebot_v11.core.instance import clear_instance_cache, resolve_instance_key
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.console import InstanceUpdateRequest
from nekro_agent.services import unclaimed_dispatch
from nekro_agent.services.console import container_manager, instance_manager
from nekro_agent.services.unclaimed_dispatch import is_inbound_dispatch_allowed
from nekro_agent.services.user import token_exposure

ONEBOT = "onebot_v11"
DEPLOYMENT_TOKEN = "deployment-token"
PLATFORM_TOKEN = "platform-own-token"
TENANT_TOKEN = "tenant-own-token"
ACCOUNT = "40001"
TENANT_ID = 7
PLATFORM = "plat"
TENANT = "tnt"

# 后台动作（断开连接这类）给多久时间落地；修复前这些断言就是等满这么久之后失败
SETTLE_SECONDS = 1.0


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
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


async def add_instance(
    instance_key: str,
    *,
    provider: str = "remote_onebot",
    token: str = "",
    account: str = "",
    tenant_id: Optional[int] = None,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ONEBOT,
        instance_key=instance_key,
        display_name=instance_key,
        provider=provider,
        provider_account_id=account,
        status="unbound" if not account else "offline",
        enabled=True,
        tenant_id=tenant_id,
        metadata_json=json.dumps({"access_token": token}) if token else "",
    )


# ---------------------------------------------------------------------------
# 真 NoneBot 适配器 + 真握手中间件
# ---------------------------------------------------------------------------


@dataclass
class Dispatched:
    """一条入站事件走完匹配器开头两步之后的结论"""

    self_id: str
    group_id: int
    instance_key: str
    dispatched: bool


class Stack:
    """一个 NoneBot OneBot V11 适配器挂在握手中间件后面，事件派发处换成记录器"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, deployment_token: str) -> None:
        set_deployment_token(monkeypatch, deployment_token)
        # 适配器在构造时读插件配置；不初始化 NoneBot 全局状态，直接给它一份
        monkeypatch.setattr(
            onebot_adapter_module,
            "get_plugin_config",
            lambda _cls: OneBotConfig(onebot_access_token=deployment_token or None),
        )
        self.driver = FastAPIDriver(Env(environment="prod"), NoneBotConfig())
        self.adapter = OneBotAdapter(self.driver)
        self.app = OneBotAccessTokenMiddleware(self.driver.asgi)
        # 生产里中间件经 `nonebot.get_adapter` 找到这个适配器；这里没有初始化 NoneBot 的全局状态
        monkeypatch.setattr(gate, "_nonebot_onebot_adapter", lambda: self.adapter)
        self.events: List[Dispatched] = []
        stack = self

        async def record(bot: OneBotBot, event: Any) -> None:
            # 与 `matchers/message.py` 群消息匹配器的开头一致
            instance_key = await resolve_instance_key(bot.self_id)
            allowed = await is_inbound_dispatch_allowed(ONEBOT, instance_key)
            stack.events.append(Dispatched(bot.self_id, int(getattr(event, "group_id", 0)), instance_key, allowed))

        monkeypatch.setattr(OneBotBot, "handle_event", record)

    async def drain(self) -> None:
        """等这一刻已经派发出去的事件处理完"""
        while self.adapter.tasks:
            await asyncio.gather(*list(self.adapter.tasks), return_exceptions=True)

    def dispatched_as(self, instance_key: str, group_id: int) -> List[Dispatched]:
        return [e for e in self.events if e.instance_key == instance_key and e.group_id == group_id and e.dispatched]


def group_message_payload(group_id: int, text: str = "hello") -> Dict[str, Any]:
    return {
        "time": int(time.time()),
        "self_id": int(ACCOUNT),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 1,
        "group_id": group_id,
        "user_id": 555,
        "anonymous": None,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": 555, "nickname": "someone", "card": "", "role": "member"},
    }


async def http_report(stack: Stack, token: Optional[str], payload: Dict[str, Any]) -> int:
    """协议端走 HTTP 上报 POST 一条事件，返回状态码"""
    headers = [(b"host", b"testserver"), (b"x-self-id", ACCOUNT.encode("latin-1")), (b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    scope: Dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50001),
        "root_path": "",
        "path": "/onebot/v11/",
        "raw_path": b"/onebot/v11/",
        "query_string": b"",
        "headers": headers,
        "state": {},
    }
    pending = [{"type": "http.request", "body": json.dumps(payload).encode("utf-8"), "more_body": False}]
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    await stack.app(scope, receive, send)
    await stack.drain()
    return int(next(message for message in sent if message["type"] == "http.response.start")["status"])


class Client:
    """一个协议端：握手、发事件、看服务端有没有把它断开

    服务端发出 `websocket.close` 之后，下一次 `receive` 给出 `websocket.disconnect`，与 uvicorn 一致。
    `ignore_close=True` 是不回应关闭帧的对端：uvicorn 的 websockets 实现会等它到关闭超时，这期间它发来的帧
    照样交给应用。
    """

    def __init__(self, stack: Stack, self_id: str, token: Optional[str], *, ignore_close: bool = False) -> None:
        self.stack = stack
        self.self_id = self_id
        self.ignore_close = ignore_close
        self.inbox: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.sent: List[Dict[str, Any]] = []
        self.accepted = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_reason = ""
        headers = [(b"host", b"testserver"), (b"x-self-id", self_id.encode("latin-1"))]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
        self.scope: Dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
            "path": "/onebot/v11/ws",
            "raw_path": b"/onebot/v11/ws",
            "query_string": b"",
            "headers": headers,
            "subprotocols": [],
            "state": {},
        }
        self.task: Optional["asyncio.Task[None]"] = None

    async def _send(self, message: Dict[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "websocket.accept":
            self.accepted.set()
        elif message["type"] == "websocket.close":
            self.close_reason = str(message.get("reason") or "")
            if not self.closed.is_set():
                self.closed.set()
                if not self.ignore_close:
                    await self.inbox.put({"type": "websocket.disconnect", "code": int(message.get("code") or 1000)})

    async def connect(self) -> bool:
        """握手；返回服务端有没有接受"""
        await self.inbox.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.stack.app(self.scope, self.inbox.get, self._send))
        accepted = asyncio.create_task(self.accepted.wait())
        closed = asyncio.create_task(self.closed.wait())
        await asyncio.wait({accepted, closed, self.task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        accepted.cancel()
        closed.cancel()
        return self.accepted.is_set() and not self.closed.is_set()

    async def group_message(self, group_id: int, text: str = "hello") -> None:
        payload = group_message_payload(group_id, text)
        if not self.closed.is_set() or self.ignore_close:
            await self.inbox.put({"type": "websocket.receive", "text": json.dumps(payload)})
        # 让适配器读到这一帧、把事件派发出去，再等派发出去的处理完
        for _ in range(5):
            await asyncio.sleep(0)
        await self.stack.drain()

    async def wait_closed(self, timeout: float = SETTLE_SECONDS) -> bool:
        try:
            await asyncio.wait_for(self.closed.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def hang_up(self) -> None:
        if self.task is None:
            return
        if not self.task.done() and (not self.closed.is_set() or self.ignore_close):
            await self.inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait({self.task}, timeout=5)


@pytest.fixture
async def clients():
    opened: List[Client] = []
    yield opened
    for client in opened:
        await client.hang_up()


def open_client(clients: List[Client], stack: Stack, token: Optional[str]) -> Client:
    client = Client(stack, ACCOUNT, token)
    clients.append(client)
    return client


async def settle() -> None:
    """给账号绑定之后的后台动作一点时间"""
    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def bind(instance: DBAdapterInstance) -> None:
    """平台方在总后台把账号填进实例"""
    fresh = await DBAdapterInstance.get(id=instance.id)
    await instance_manager.update_instance(fresh, InstanceUpdateRequest(provider_account_id=ACCOUNT))


# ---------------------------------------------------------------------------
# 复现：租户的连接抢先占住平台方即将绑定的账号
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestTakeover:
    async def test_tenant_awaiting_token_then_platform_binds_in_console(self, monkeypatch, clients) -> None:
        """原始报告的那一格：租户用自家待绑实例的令牌抢先连上，平台方随后在总后台把账号绑到自己的实例"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        tenant = open_client(clients, stack, TENANT_TOKEN)
        assert await tenant.connect(), "前提：账号未绑定时，租户待绑实例的令牌能握手成功"

        await bind(platform)
        await settle()

        # 断言 1：租户那条连接被断开
        assert await tenant.wait_closed(), "账号绑到平台实例之后，凭租户待绑实例令牌握手的连接仍然挂着"
        # 断言 2：它发来的消息不会被当成平台实例的消息分发
        await tenant.group_message(1001, "forged")
        assert stack.dispatched_as(PLATFORM, 1001) == [], "租户连接发来的消息被当成平台实例的消息分发"
        # 断言 3：平台方真正的协议端能连上（不再被 Duplicate X-Self-ID 拒掉）
        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"

    async def test_empty_deployment_token_needs_no_token_at_all(self, monkeypatch, clients) -> None:
        """部署级令牌为空：账号未绑定时空令牌也能握手，同样的后果"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        stack = Stack(monkeypatch, "")

        tenant = open_client(clients, stack, None)
        assert await tenant.connect()

        await bind(platform)
        await settle()

        assert await tenant.wait_closed(), "空令牌握手的连接在账号绑到有令牌的平台实例之后仍然挂着"
        await tenant.group_message(1001, "forged")
        assert stack.dispatched_as(PLATFORM, 1001) == []
        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"

    async def test_builtin_napcat_login_attribution_triggered_by_the_tenant_connection(self, monkeypatch, clients) -> None:
        """内置 NapCat：平台方刚扫码登录，租户的连接先到；它的第一条事件触发登录归属推导，把账号绑给平台实例

        这条事件本身、以及之后的事件，都不能当成平台实例的消息。
        """
        platform = await add_instance(PLATFORM, provider="builtin_napcat", token=PLATFORM_TOKEN)
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        async def logged_in(instance: DBAdapterInstance) -> str:
            return ACCOUNT if instance.id == platform.id else ""

        monkeypatch.setattr(container_manager, "read_container_login_account", logged_in)

        tenant = open_client(clients, stack, TENANT_TOKEN)
        assert await tenant.connect()
        await tenant.group_message(1001, "first")
        assert (await DBAdapterInstance.get(id=platform.id)).provider_account_id == ACCOUNT, "前提：归属推导把账号绑给了平台实例"
        await tenant.group_message(1002, "second")
        await settle()

        assert stack.dispatched_as(PLATFORM, 1001) == [], "触发归属推导的那条租户事件被当成平台实例的消息分发"
        assert stack.dispatched_as(PLATFORM, 1002) == [], "归属推导之后租户连接的事件被当成平台实例的消息分发"
        assert await tenant.wait_closed()
        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"

    async def test_tenant_connection_registered_as_unclaimed_then_merged_into_the_platform_instance(
        self, monkeypatch, clients,
    ) -> None:
        """租户的连接先发了消息，被登记成未认领实例；平台方把它并入自己的实例（认领流程）"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        tenant = open_client(clients, stack, TENANT_TOKEN)
        assert await tenant.connect()
        await tenant.group_message(1001, "before")
        source = await DBAdapterInstance.get(adapter_key=ONEBOT, provider_account_id=ACCOUNT)
        assert source.status == "unclaimed"
        assert stack.dispatched_as(source.instance_key, 1001) == [], "前提：未认领闸门不分发备选令牌连接的消息"

        await instance_manager.adopt_unclaimed_instance(source, await DBAdapterInstance.get(id=platform.id))
        await settle()

        assert await tenant.wait_closed()
        await tenant.group_message(1002, "after")
        assert stack.dispatched_as(PLATFORM, 1002) == []
        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"


@pytest.mark.usefixtures("db")
class TestTakeoverSidePaths:
    """独立复核补出来的旁路"""

    async def test_http_report_before_binding_does_not_lock_out_the_real_endpoint(self, monkeypatch, clients) -> None:
        """账号未绑定时凭租户待绑实例的令牌 POST 一条事件：NoneBot 为它建一个没有连接、永不移除的 Bot，
        平台方并入之后，真协议端的 WS 不能一直被判成重复"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        assert await http_report(stack, TENANT_TOKEN, group_message_payload(1001)) == 204
        assert ACCOUNT in stack.adapter.bots
        assert ACCOUNT not in stack.adapter.connections, "前提：HTTP 上报留下了一个没有连接的 Bot"
        source = await DBAdapterInstance.get(adapter_key=ONEBOT, provider_account_id=ACCOUNT)
        await instance_manager.adopt_unclaimed_instance(source, await DBAdapterInstance.get(id=platform.id))
        await settle()

        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"
        await real.group_message(1002)
        assert len(stack.dispatched_as(PLATFORM, 1002)) == 1

    async def test_platform_claims_the_tenant_connection_in_place(self, monkeypatch, clients) -> None:
        """租户一连上就被登记成未认领实例（生产里 `on_bot_connect` 当场解析实例），平台方以为是自己的协议端，就地认领。
        认领后的实例没有自己的令牌，应出示部署级令牌；租户出示的是自家待绑实例的令牌，连接要断开"""
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        tenant = open_client(clients, stack, TENANT_TOKEN)
        assert await tenant.connect()
        await resolve_instance_key(ACCOUNT)
        source = await DBAdapterInstance.get(adapter_key=ONEBOT, provider_account_id=ACCOUNT)
        assert source.status == "unclaimed"

        await instance_manager.claim_unclaimed_as_new_instance(source, "我的 Bot", "")
        await settle()

        assert await tenant.wait_closed(), "就地认领之后，凭别家待绑实例令牌握手的连接仍然挂着"
        real = open_client(clients, stack, DEPLOYMENT_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"

    async def test_binding_that_lands_while_the_handshake_is_being_checked(self, monkeypatch, clients) -> None:
        """绑定恰好落在握手查库与连接登记之间、复核也已经跑完：这条连接仍要被复核到"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)
        original = gate._awaiting_account_tokens  # noqa: SLF001

        async def bind_meanwhile() -> Any:
            tokens = await original()
            await bind(platform)
            await gate.wait_for_pending_revalidation()
            return tokens

        monkeypatch.setattr(gate, "_awaiting_account_tokens", bind_meanwhile)
        tenant = open_client(clients, stack, TENANT_TOKEN)
        await tenant.connect()
        monkeypatch.setattr(gate, "_awaiting_account_tokens", original)
        await settle()

        assert await tenant.wait_closed(), "握手期间完成的绑定没有复核到这条连接"
        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect(), f"平台方真正的协议端被拒：{real.close_reason}"

    async def test_token_rotation_that_lands_while_the_handshake_is_being_checked(self, monkeypatch, clients) -> None:
        """换令牌直接调复核、不经过排队：握手已按旧令牌查完、还没登记时换掉，这条连接也要被复核到"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN, account=ACCOUNT)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)
        original = gate.resolve_expected_token

        async def rotate_meanwhile(self_id: str) -> Any:
            expected = await original(self_id)
            await instance_manager.rotate_access_token(await DBAdapterInstance.get(id=platform.id), operator="用例")
            return expected

        monkeypatch.setattr(gate, "resolve_expected_token", rotate_meanwhile)
        stale = open_client(clients, stack, PLATFORM_TOKEN)
        await stale.connect()
        monkeypatch.setattr(gate, "resolve_expected_token", original)
        await settle()

        assert await stale.wait_closed(), "握手期间换掉的令牌，凭它连上来的连接没有被复核到"
        new_token = json.loads((await DBAdapterInstance.get(id=platform.id)).metadata_json)["access_token"]
        fresh = open_client(clients, stack, new_token)
        assert await fresh.connect(), f"凭新令牌重连被拒：{fresh.close_reason}"

    async def test_frames_after_the_close_frame_are_still_dropped(self, monkeypatch, clients) -> None:
        """对端不回应关闭帧：这段时间里它发来的帧照样被派发到事件处理，入站闸门要挡住"""
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        tenant = Client(stack, ACCOUNT, TENANT_TOKEN, ignore_close=True)
        clients.append(tenant)
        assert await tenant.connect()
        await bind(platform)
        await settle()
        assert tenant.closed.is_set(), "前提：关闭帧已经发出"

        await tenant.group_message(1001, "after close")
        assert [e for e in stack.events if e.group_id == 1001], "前提：对端不理关闭帧时，它的帧仍被派发"
        assert stack.dispatched_as(PLATFORM, 1001) == []


# ---------------------------------------------------------------------------
# 对照：握手与绑定目标对得上的连接一个都不能断
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestLegitimateConnectionsSurvive:
    async def test_platform_protocol_end_on_its_own_token_survives_binding(self, monkeypatch, clients) -> None:
        platform = await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect()
        await bind(platform)
        await settle()

        assert not real.closed.is_set()
        await real.group_message(1001)
        assert len(stack.dispatched_as(PLATFORM, 1001)) == 1

    async def test_tenant_protocol_end_bound_to_its_own_instance_survives(self, monkeypatch, clients) -> None:
        tenant_row = await add_instance(TENANT, token=TENANT_TOKEN, tenant_id=TENANT_ID)
        await add_instance(PLATFORM, token=PLATFORM_TOKEN)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        tenant = open_client(clients, stack, TENANT_TOKEN)
        assert await tenant.connect()
        await bind(tenant_row)
        await settle()

        assert not tenant.closed.is_set()
        await tenant.group_message(1001)
        assert len(stack.dispatched_as(TENANT, 1001)) == 1

    async def test_builtin_napcat_first_login_needs_no_reconnect(self, monkeypatch, clients) -> None:
        """平台方新建的内置 NapCat 扫码登录后，凭自己的令牌连上来、第一条事件触发归属推导：照常分发，不断开"""
        platform = await add_instance(PLATFORM, provider="builtin_napcat", token=PLATFORM_TOKEN)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        async def logged_in(instance: DBAdapterInstance) -> str:
            return ACCOUNT if instance.id == platform.id else ""

        monkeypatch.setattr(container_manager, "read_container_login_account", logged_in)

        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect()
        await real.group_message(1001)
        await settle()

        assert len(stack.dispatched_as(PLATFORM, 1001)) == 1
        assert not real.closed.is_set()

    async def test_builtin_napcat_first_login_on_an_empty_deployment_token(self, monkeypatch, clients) -> None:
        """部署级令牌为空时握手不校验，但内置 NapCat 照样带着自己的令牌：归属推导之后也不该被断开"""
        platform = await add_instance(PLATFORM, provider="builtin_napcat", token=PLATFORM_TOKEN)
        stack = Stack(monkeypatch, "")

        async def logged_in(instance: DBAdapterInstance) -> str:
            return ACCOUNT if instance.id == platform.id else ""

        monkeypatch.setattr(container_manager, "read_container_login_account", logged_in)

        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect()
        await real.group_message(1001)
        await settle()

        assert len(stack.dispatched_as(PLATFORM, 1001)) == 1
        assert not real.closed.is_set()

    async def test_instance_relying_on_the_deployment_token_keeps_its_connection(self, monkeypatch, clients) -> None:
        """实例没有自己的令牌、协议端凭部署级令牌连上来：绑定之后期望令牌就是它出示的那枚"""
        platform = await add_instance(PLATFORM)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        real = open_client(clients, stack, DEPLOYMENT_TOKEN)
        assert await real.connect()
        await bind(platform)
        await settle()

        assert not real.closed.is_set()
        await real.group_message(1001)
        assert len(stack.dispatched_as(PLATFORM, 1001)) == 1

    async def test_a_second_connection_does_not_push_out_a_live_one(self, monkeypatch, clients) -> None:
        """清理只针对 HTTP 上报留下的、没有连接的 Bot：账号已经有一条 WS 时，第二条照旧按重复拒绝"""
        await add_instance(PLATFORM, token=PLATFORM_TOKEN, account=ACCOUNT)
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        first = open_client(clients, stack, PLATFORM_TOKEN)
        assert await first.connect()
        second = open_client(clients, stack, PLATFORM_TOKEN)
        assert not await second.connect()
        assert second.close_reason == "Duplicate X-Self-ID"

        assert not first.closed.is_set()
        await first.group_message(1001)
        assert len(stack.dispatched_as(PLATFORM, 1001)) == 1

    async def test_binding_another_account_leaves_this_connection_alone(self, monkeypatch, clients) -> None:
        await add_instance(PLATFORM, token=PLATFORM_TOKEN, account=ACCOUNT)
        other = await add_instance("other", token="other-token")
        stack = Stack(monkeypatch, DEPLOYMENT_TOKEN)

        real = open_client(clients, stack, PLATFORM_TOKEN)
        assert await real.connect()
        fresh = await DBAdapterInstance.get(id=other.id)
        await instance_manager.update_instance(fresh, InstanceUpdateRequest(provider_account_id="49999"))
        await settle()

        assert not real.closed.is_set()
        await real.group_message(1001)
        assert len(stack.dispatched_as(PLATFORM, 1001)) == 1
