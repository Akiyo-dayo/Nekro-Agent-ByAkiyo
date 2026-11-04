"""`POST /chat-channel/detail/{chat_key}/refresh`：向平台实时取频道名（`9310a39`）

本分支与上游的差别集中在**取名字的方式**（设计 §3.5）：上游直接调
`DBChatChannel.sync_channel_name()`，它依赖调用链路的实例上下文、取失败时把
`channel_id` 当名字写回去、并且整行 `save()`。这里钉住三条替代行为：

- 名字按频道所属实例取（OneBot 在新任务里设账号上下文，其他适配器用 `instance_scope`）；
- 取到空串或等于 `channel_id` 算失败，保留原名；
- 只写 `channel_name` 与 `update_time` 两列，其他列（`data` / `status`）不被冲回。
"""

import json
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.routers import chat_channel as cc_router


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def make_channel(
    *,
    adapter_key: str = "onebot_v11",
    instance_key: str = "inst1",
    channel_id: str = "group_123456",
    channel_name: str = "旧名字",
    data: Optional[Dict[str, Any]] = None,
) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        channel_id=channel_id,
        chat_key=f"{adapter_key}-{instance_key}-{channel_id}" if instance_key else f"{adapter_key}-{channel_id}",
        channel_type="group",
        channel_name=channel_name,
        data=json.dumps(data or {}),
        is_active=True,
        observe_mode=False,
    )


class _FakeAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: List[str] = []

    async def get_channel_info(self, channel_id: str) -> Any:
        self.queries.append(channel_id)

        class _Info:
            channel_name = "新名字"
            instance_key = ""

        return _Info()


@pytest.fixture
def onebot_account(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """假装实例 `inst1` 绑着账号 10001，并记录取名字时上下文里的账号"""
    seen: List[Any] = []

    monkeypatch.setattr(
        "nekro_agent.adapters.onebot_v11.core.instance.lookup_cached_account_id",
        lambda instance_key: "10001" if instance_key == "inst1" else None,
    )

    async def fake_resolve(instance_key: str) -> Optional[str]:
        return "10001" if instance_key == "inst1" else None

    monkeypatch.setattr("nekro_agent.adapters.onebot_v11.core.instance.resolve_account_id", fake_resolve)

    adapter = _FakeAdapter("onebot_v11")

    async def fake_get_channel_info(channel_id: str) -> Any:
        from nekro_agent.adapters.onebot_v11.core.instance import get_current_instance

        context = get_current_instance()
        seen.append((channel_id, context.account_id if context else None, context.instance_key if context else None))
        adapter.queries.append(channel_id)

        class _Info:
            channel_name = "新名字"

        return _Info()

    adapter.get_channel_info = fake_get_channel_info  # type: ignore[method-assign]
    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: adapter)
    return seen


async def test_refresh_writes_the_name_from_the_owning_account(
    db: None,
    onebot_account: List[Any],
) -> None:
    channel = await make_channel()

    await cc_router._refresh_channel_name(channel)

    assert channel.channel_name == "新名字"
    # 关键：问的是这个频道自己的账号，不是「碰巧在线的那一个」
    assert onebot_account == [("group_123456", "10001", "inst1")]
    assert (await DBChatChannel.get(id=channel.id)).channel_name == "新名字"


async def test_refresh_does_not_overwrite_other_columns(db: None, onebot_account: List[Any]) -> None:
    """整行 `save()` 会把并发改的 `data` / `status` 冲回旧值"""
    channel = await make_channel(data={"custom_channel_name": "我自己起的名"})

    # 模拟并发：另一次请求改了同一行的 data
    await DBChatChannel.filter(id=channel.id).update(data=json.dumps({"custom_channel_name": "并发改的名"}))
    channel.channel_name = "旧名字"

    await cc_router._refresh_channel_name(channel)

    fresh = await DBChatChannel.get(id=channel.id)
    assert fresh.channel_name == "新名字"
    assert json.loads(fresh.data)["custom_channel_name"] == "并发改的名"


async def test_channel_id_fallback_is_treated_as_failure(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """OneBot 取失败时把 `channel_id` 当名字返回：这是失败，不能拿它覆盖真名"""

    class _Adapter:
        async def get_channel_info(self, _channel_id: str) -> Any:
            class _Info:
                channel_name = "group_123456"

            return _Info()

    monkeypatch.setattr(
        "nekro_agent.adapters.onebot_v11.core.instance.lookup_cached_account_id",
        lambda _key: "10001",
    )
    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())

    channel = await make_channel(channel_name="真名")

    await cc_router._refresh_channel_name(channel)

    assert (await DBChatChannel.get(id=channel.id)).channel_name == "真名"


async def test_empty_name_is_treated_as_failure(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Adapter:
        async def get_channel_info(self, _channel_id: str) -> Any:
            class _Info:
                channel_name = "   "

            return _Info()

    monkeypatch.setattr(
        "nekro_agent.adapters.onebot_v11.core.instance.lookup_cached_account_id",
        lambda _key: "10001",
    )
    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())

    channel = await make_channel(channel_name="真名")

    await cc_router._refresh_channel_name(channel)

    assert (await DBChatChannel.get(id=channel.id)).channel_name == "真名"


async def test_unknown_account_does_not_ask_anyone(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """实例查不到账号就放弃：无参 `get_bot()` 会回落到唯一在线账号，那可能是别人的号"""
    asked: List[str] = []

    class _Adapter:
        async def get_channel_info(self, channel_id: str) -> Any:
            asked.append(channel_id)

            class _Info:
                channel_name = "别人的群名"

            return _Info()

    monkeypatch.setattr(
        "nekro_agent.adapters.onebot_v11.core.instance.lookup_cached_account_id",
        lambda _key: None,
    )

    async def fake_resolve(_key: str) -> Optional[str]:
        return None

    monkeypatch.setattr("nekro_agent.adapters.onebot_v11.core.instance.resolve_account_id", fake_resolve)
    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())

    channel = await make_channel(instance_key="ghost", channel_name="真名")

    await cc_router._refresh_channel_name(channel)

    assert asked == []
    assert (await DBChatChannel.get(id=channel.id)).channel_name == "真名"


async def test_other_adapters_use_the_instance_scope(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.plugin.scope import current_instance_key

    observed: List[str] = []

    class _Adapter:
        async def get_channel_info(self, _channel_id: str) -> Any:
            observed.append(current_instance_key())

            class _Info:
                channel_name = "别的平台的名字"

            return _Info()

    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())

    channel = await make_channel(adapter_key="web", instance_key="", channel_id="private_7", channel_name="旧名字")

    await cc_router._refresh_channel_name(channel)

    assert observed == [""]
    assert (await DBChatChannel.get(id=channel.id)).channel_name == "别的平台的名字"


async def test_long_name_is_cut_to_the_column_width_and_broadcast(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram 一类平台的群名可以超过 64 字：截到列宽再写，写成功后广播给频道列表"""
    long_name = "很长的群名" * 20
    published: List[Dict[str, Any]] = []

    class _Adapter:
        async def get_channel_info(self, _channel_id: str) -> Any:
            class _Info:
                channel_name = long_name

            return _Info()

    async def fake_publish(**kwargs: Any) -> None:
        published.append(kwargs)

    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())
    monkeypatch.setattr(cc_router.channel_broadcaster, "publish_update", fake_publish)

    channel = await make_channel(adapter_key="telegram", instance_key="", channel_id="group_9", channel_name="旧名字")

    await cc_router._refresh_channel_name(channel)

    assert (await DBChatChannel.get(id=channel.id)).channel_name == long_name[:64]
    assert len(published) == 1
    assert published[0]["event_type"] == "updated"
    assert published[0]["channel_name"] == long_name[:64]


async def test_adapter_failure_keeps_the_old_name_and_does_not_raise(
    db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter:
        async def get_channel_info(self, _channel_id: str) -> Any:
            raise RuntimeError("协议端掉线了")

    monkeypatch.setattr(
        "nekro_agent.adapters.onebot_v11.core.instance.lookup_cached_account_id",
        lambda _key: "10001",
    )
    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())

    channel = await make_channel(channel_name="真名")

    await cc_router._refresh_channel_name(channel)  # 不抛

    assert (await DBChatChannel.get(id=channel.id)).channel_name == "真名"


async def test_missing_adapter_keeps_the_old_name(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_key: str) -> Any:
        raise RuntimeError("适配器未加载")

    monkeypatch.setattr(cc_router, "get_adapter", boom)

    channel = await make_channel(adapter_key="wechat_openilink", channel_name="真名")

    await cc_router._refresh_channel_name(channel)

    assert (await DBChatChannel.get(id=channel.id)).channel_name == "真名"


async def test_unchanged_name_does_not_bump_update_time(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """名字没变就不写：否则每次点刷新都把 `update_time` 推一下"""
    class _Adapter:
        async def get_channel_info(self, _channel_id: str) -> Any:
            class _Info:
                channel_name = "同一个名字"

            return _Info()

    monkeypatch.setattr(
        "nekro_agent.adapters.onebot_v11.core.instance.lookup_cached_account_id",
        lambda _key: "10001",
    )
    monkeypatch.setattr(cc_router, "get_adapter", lambda _key: _Adapter())

    channel = await make_channel(channel_name="同一个名字")
    before = channel.update_time

    await cc_router._refresh_channel_name(channel)

    assert (await DBChatChannel.get(id=channel.id)).update_time == before


async def test_detail_builder_is_shared_by_both_endpoints(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个端点必须用同一份构建逻辑，否则刷新后的响应会和详情页对不上"""
    import ast
    import inspect

    for name in ("get_chat_channel_detail", "refresh_chat_channel_detail"):
        source = inspect.getsource(getattr(cc_router, name).__wrapped__)  # type: ignore[attr-defined]
        tree = ast.parse(source)
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert "_build_chat_channel_detail" in calls, name


async def test_refresh_endpoint_checks_tenant_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨租户的 chat_key 必须 404，先于任何「取名字」的动作"""
    from nekro_agent.models.db_user import DBUser
    from nekro_agent.schemas.errors import NotFoundError

    tried: List[str] = []

    async def fake_accessible(_user: DBUser, chat_key: str) -> None:
        raise NotFoundError(resource="聊天频道")

    async def fake_refresh(channel: DBChatChannel) -> None:
        tried.append(channel.chat_key)

    monkeypatch.setattr(cc_router, "assert_channel_accessible", fake_accessible)
    monkeypatch.setattr(cc_router, "_refresh_channel_name", fake_refresh)

    # 直接调 `__wrapped__`：`require_role` 那一层要真实的登录态，这里只验端点正文的判定顺序
    endpoint = cc_router.refresh_chat_channel_detail.__wrapped__  # type: ignore[attr-defined]

    with pytest.raises(NotFoundError):
        await endpoint("onebot_v11-inst2-group_9", _current_user=object())

    assert tried == []


def test_refresh_route_is_declared_admin() -> None:
    """整块频道路由的门槛是 `Role.Admin`；刷新端点不能例外"""
    import ast
    import inspect

    source = inspect.getsource(cc_router.refresh_chat_channel_detail.__wrapped__)  # type: ignore[attr-defined]
    decorators = source.split("async def", 1)[0]

    assert "@require_role(Role.Admin)" in decorators
    assert "router.post(" in decorators
    assert ast.literal_eval(decorators.split("router.post(", 1)[1].split(",", 1)[0]) == "/detail/{chat_key}/refresh"
