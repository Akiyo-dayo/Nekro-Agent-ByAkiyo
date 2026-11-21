"""插件后台行为按实例 / 按频道判定

这些用例钉的是同一件事：后台推送、消息钩子、命令、定时提醒、节日广播、Webhook 订阅、
CC 结果投递与心声布防，都不能只看「插件在进程里活着」，而要按 会话 > 实例 > 全局 判它在
目标频道是否生效。判不出来时（多租户部署里频道行缺失）一律按不生效处理。
"""

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_recurring_timer_job import DBRecurringTimerJob
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.command.base import BaseCommand, CommandMetadata, CommandPermission
from nekro_agent.services.command.manager import command_manager
from nekro_agent.services.command.registry import command_registry
from nekro_agent.services.command.schemas import (
    CommandExecutionContext,
    CommandRequest,
    CommandResponse,
    CommandResponseStatus,
)
from nekro_agent.services.message_service import message_service
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.scope import (
    OverrideScope,
    plugin_effective_in_chat,
    plugin_effective_with_instance,
    resolve_chat_plugin_instance,
    set_scope_override,
)

ADAPTER = "onebot_v11"
INST1 = "inst1"
INST2 = "inst2"
CHAT1 = "onebot_v11-inst1-group_1"
CHAT2 = "onebot_v11-inst2-group_2"


async def _fake_enqueue(*_args: Any, **_kwargs: Any) -> None:
    """节日邮件入队是另一条独立的路：本文件的用例只测群推送的作用域"""
    return None


class _FakeTask:
    """`asyncio.create_task` 的替身：登记回调时立刻回调，任务集合不会积压"""

    def __init__(self, coro: Any) -> None:
        coro.close()

    def add_done_callback(self, callback: Any) -> None:
        callback(self)

    def cancelled(self) -> bool:
        return False

    def exception(self) -> None:
        return None


def _fake_create_task(coro: Any) -> _FakeTask:
    return _FakeTask(coro)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def scope_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """覆盖文件改道到临时目录，避免写进开发机的真实数据目录"""
    data_dir = tmp_path / "plugin_data"
    monkeypatch.setattr(scope_mod, "PLUGIN_DATA_DIR", data_dir)
    monkeypatch.setattr(scope_mod, "SCOPE_OVERRIDE_DIR", data_dir / "_overrides")
    scope_mod._scope_override_cache.clear()  # noqa: SLF001
    scope_mod._scoped_config_cache.clear()  # noqa: SLF001
    try:
        yield
    finally:
        scope_mod._scope_override_cache.clear()  # noqa: SLF001
        scope_mod._scoped_config_cache.clear()  # noqa: SLF001


def set_global(monkeypatch: pytest.MonkeyPatch, key: str, enabled: bool) -> None:
    keys = [item for item in config.PLUGIN_ENABLED if item != key]
    if enabled:
        keys.append(key)
    monkeypatch.setattr(config, "PLUGIN_ENABLED", keys)


async def make_instance(instance_key: str, *, tenant_id: Optional[int] = None, status: str = "online") -> None:
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
        provider="remote_onebot",
        tenant_id=tenant_id,
    )


async def make_channel(chat_key: str, instance_key: str, *, is_active: bool = True) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        chat_key=chat_key,
        channel_type=ChatType.GROUP.value,
        is_active=is_active,
        data="{}",
    )


def make_plugin(module_name: str = "scope_probe", author: str = "Probe") -> NekroPlugin:
    return NekroPlugin(
        name="作用域探针",
        module_name=module_name,
        description="测试用插件",
        version="1.0.0",
        author=author,
        url="",
    )


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------


class TestResolveChatInstance:
    async def test_a_channel_row_gives_its_instance(self, db, scope_env) -> None:
        del scope_env
        await make_channel(CHAT1, INST1)

        assert await resolve_chat_plugin_instance(CHAT1) == INST1

    async def test_a_missing_row_in_a_single_tenant_deployment_is_empty(self, db, scope_env) -> None:
        del scope_env
        await make_instance(INST1)

        assert await resolve_chat_plugin_instance("onebot_v11-inst1-group_404") == ""

    async def test_a_missing_row_in_a_multi_tenant_deployment_is_none(self, db, scope_env) -> None:
        del scope_env
        await DBTenant.create(id=1, name="t1", display_name="t1")
        await make_instance(INST1, tenant_id=1)

        assert await resolve_chat_plugin_instance("onebot_v11-inst1-group_404") is None

    async def test_an_empty_chat_key_cannot_be_resolved(self, db, scope_env) -> None:
        del scope_env
        assert await resolve_chat_plugin_instance("") is None


class TestEffectiveInChat:
    async def test_session_beats_instance_and_instance_beats_global(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key = "Probe.scope_probe"
        del scope_env
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)

        # 全局关、实例开、会话关：会话最优先
        set_global(monkeypatch, key, False)
        set_scope_override(OverrideScope.INSTANCE, INST1, key, True)
        set_scope_override(OverrideScope.SESSION, CHAT1, key, False)
        assert not await plugin_effective_in_chat(key, CHAT1)

        # 去掉会话覆盖后回落到实例：开
        set_scope_override(OverrideScope.SESSION, CHAT1, key, None)
        assert await plugin_effective_in_chat(key, CHAT1)

        # 没有实例覆盖时回落到全局：关
        set_scope_override(OverrideScope.INSTANCE, INST1, key, None)
        assert not await plugin_effective_in_chat(key, CHAT1)

    async def test_an_unresolvable_instance_is_not_effective(self, db, scope_env) -> None:
        del scope_env
        assert not plugin_effective_with_instance("Probe.scope_probe", None, "onebot_v11-inst1-group_1")


# ---------------------------------------------------------------------------
# 消息钩子
# ---------------------------------------------------------------------------


class TestMessageHooks:
    async def test_a_channel_where_the_plugin_is_off_does_not_run_it(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.plugin.collector import plugin_collector

        del scope_env
        plugin = make_plugin()
        calls: List[str] = []

        async def hook(_ctx: AgentCtx, _message: ChatMessage) -> None:
            calls.append("called")

        plugin.on_user_message_method = hook
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, False)
        monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)

        ctx = AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER)
        await plugin_collector.handle_on_user_message(ctx, ChatMessage.create_empty(CHAT1))
        assert calls == []

        set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, True)
        await plugin_collector.handle_on_user_message(ctx, ChatMessage.create_empty(CHAT1))
        assert calls == ["called"]

    async def test_the_reset_hook_uses_the_same_criterion(self, db, scope_env, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.plugin.collector import plugin_collector

        del scope_env
        plugin = make_plugin("reset_probe")
        calls: List[str] = []

        async def hook(_ctx: AgentCtx) -> None:
            calls.append("reset")

        plugin.on_reset_method = hook
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, False)
        monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)

        ctx = AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER)
        await plugin_collector.chat_channel_on_reset(ctx)
        assert calls == []


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


class _ProbeCommand(BaseCommand):
    def __init__(self, meta: CommandMetadata) -> None:
        self._meta = meta

    @property
    def metadata(self) -> CommandMetadata:
        return self._meta

    async def execute(self, context: CommandExecutionContext, **kwargs: Any) -> CommandResponse:
        del context, kwargs
        return CommandResponse(status=CommandResponseStatus.SUCCESS, message="ok")


class TestCommands:
    async def test_plugin_commands_follow_the_channel_scope(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.plugin.collector import plugin_collector

        del scope_env
        plugin = make_plugin("command_probe")
        plugin._is_enabled = True
        meta = CommandMetadata(
            name="scope_probe_cmd",
            namespace=plugin.module_name,
            description="探针命令",
            permission=CommandPermission.PUBLIC,
            source=plugin.key,
        )
        command = _ProbeCommand(meta)
        command_registry.register(command)
        await make_instance(INST1)
        await make_instance(INST2)
        await make_channel(CHAT1, INST1)
        await make_channel(CHAT2, INST2)
        try:
            set_global(monkeypatch, plugin.key, True)
            monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)

            set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, False)
            assert not command_manager.is_command_enabled_for_meta(meta, CHAT1, chat_instance_key=INST1)
            assert command_manager.is_command_enabled_for_meta(meta, CHAT2, chat_instance_key=INST2)

            # 不传实例判据时保持旧行为：只看进程级
            assert command_manager.is_command_enabled_for_meta(meta, CHAT1)

            # 全局关、只开一个实例：命令只在这个实例的频道里可用
            set_global(monkeypatch, plugin.key, False)
            set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, True)
            assert command_manager.is_command_enabled_for_meta(meta, CHAT1, chat_instance_key=INST1)
            assert not command_manager.is_command_enabled_for_meta(meta, CHAT2, chat_instance_key=INST2)

            request = CommandRequest(
                command_name=meta.name,
                context=CommandExecutionContext(
                    user_id="1",
                    chat_key=CHAT2,
                    username="tester",
                    adapter_key=ADAPTER,
                    is_super_user=True,
                ),
            )
            responses = [item async for item in command_registry.execute(request)]
            assert responses[0].status is CommandResponseStatus.DISABLED
            assert "命令已禁用" in responses[0].message
        finally:
            command_registry.unregister_plugin_commands(plugin.key)

    async def test_export_tools_and_completion_respect_the_instance(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.command.completion import completion_provider
        from nekro_agent.services.command.tool_export import agent_tool_exporter
        from nekro_agent.services.plugin.collector import plugin_collector

        del scope_env
        plugin = make_plugin("export_probe")
        plugin._is_enabled = True
        meta = CommandMetadata(
            name="scope_export_cmd",
            namespace=plugin.module_name,
            description="导出探针命令",
            permission=CommandPermission.PUBLIC,
            source=plugin.key,
        )
        command_registry.register(_ProbeCommand(meta))
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        try:
            set_global(monkeypatch, plugin.key, True)
            monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)
            set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, False)

            names = {item["function"]["name"] for item in agent_tool_exporter.export_tools(CHAT1, chat_instance_key=INST1)}
            assert meta.name not in names
            # 不传实例判据时与改造前一致：工具仍会导出
            names_old = {item["function"]["name"] for item in agent_tool_exporter.export_tools(CHAT1)}
            assert meta.name in names_old

            completions = await completion_provider.get_completion_entries(CHAT1, chat_instance_key=INST1)
            assert meta.name not in {item.name for item in completions}
            completions_old = await completion_provider.get_completion_entries(CHAT1)
            assert meta.name in {item.name for item in completions_old}
        finally:
            command_registry.unregister_plugin_commands(plugin.key)


# ---------------------------------------------------------------------------
# 插件推送方法
# ---------------------------------------------------------------------------


class TestPushMethods:
    async def test_no_downstream_call_when_ineffective(self, db, scope_env, monkeypatch: pytest.MonkeyPatch) -> None:
        del scope_env
        plugin = make_plugin("push_probe")
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, plugin.key, False)

        calls: List[Dict[str, Any]] = []

        async def push(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(message_service, "push_system_message", push)

        assert not await plugin.push_system_message(CHAT1, "hi", trigger_agent=True)
        assert calls == []

    async def test_downstream_is_called_when_effective(self, db, scope_env, monkeypatch: pytest.MonkeyPatch) -> None:
        del scope_env
        plugin = make_plugin("push_probe")
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, plugin.key, True)

        calls: List[Dict[str, Any]] = []

        async def push(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(message_service, "push_system_message", push)

        assert await plugin.push_system_message(CHAT1, "hi", trigger_agent=True)
        assert calls and calls[0]["chat_key"] == CHAT1

    async def test_send_text_checks_the_scope_before_calling_the_api(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.api import message as message_api

        del scope_env
        plugin = make_plugin("send_probe")
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, plugin.key, True)

        calls: List[str] = []

        async def send_text(chat_key: str, message: str, ctx: AgentCtx, **_kwargs: Any) -> None:
            del message, ctx
            calls.append(chat_key)

        monkeypatch.setattr(message_api, "send_text", send_text)
        ctx = AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER)

        assert await plugin.send_text(CHAT1, "hi", ctx)
        assert calls == [CHAT1]

        set_scope_override(OverrideScope.SESSION, CHAT1, plugin.key, False)
        assert not await plugin.send_text(CHAT1, "hi", ctx)
        assert calls == [CHAT1]


# ---------------------------------------------------------------------------
# 节日祝福
# ---------------------------------------------------------------------------


async def add_recent_message(chat_key: str) -> None:
    from nekro_agent.models.db_chat_message import DBChatMessage

    now = int(datetime.now(timezone.utc).timestamp())
    await DBChatMessage.create(
        sender_id="2",
        sender_name="tester",
        sender_nickname="tester",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=chat_key.split("-")[1],
        message_id="",
        chat_key=chat_key,
        chat_type=ChatType.GROUP.value,
        platform_userid="2",
        content_text="在吗",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=now - 60,
    )


class TestFestivalGreeting:
    async def _setup(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from plugins.builtin.festival_greeting import handlers
        from plugins.builtin.festival_greeting.plugin import FestivalGreetingConfig

        await make_instance(INST1)
        await make_instance(INST2)
        await make_channel(CHAT1, INST1)
        await make_channel(CHAT2, INST2)
        await add_recent_message(CHAT1)
        await add_recent_message(CHAT2)

        greeting = FestivalGreetingConfig(
            AUTO_GREETING_ENABLED=True,
            BLACKLIST_CHATS=[],
            WHITELIST_CHATS=[],
            SKIP_INACTIVE_DAYS=30,
            RANDOM_DELAY_MAX=1,
        )
        monkeypatch.setattr(handlers.plugin, "get_config", lambda _cls: greeting)
        # 节日邮件入队是另一条路：本组只测群推送的作用域，先把入队换成空操作
        monkeypatch.setattr(handlers, "enqueue_festival_mail", _fake_enqueue)
        monkeypatch.setattr(
            handlers,
            "asyncio",
            SimpleNamespace(sleep=lambda _s: None, create_task=_fake_create_task),
        )

        pushed: List[str] = []

        async def push(**_kwargs: Any) -> None:
            pushed.append(_kwargs["chat_key"])

        # 只替下游：插件自己的 `push_system_message` 照常跑，判据才是被测的对象。
        monkeypatch.setattr(message_service, "push_system_message", push)
        return handlers, pushed

    async def test_global_off_means_no_platform_broadcast_at_all(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del scope_env
        handlers, pushed = await self._setup(monkeypatch)
        set_global(monkeypatch, handlers.plugin.key, False)
        # 全局关、但某个实例显式开着：全平台广播只由平台方的全局开关决定，整次跳过
        set_scope_override(OverrideScope.INSTANCE, INST1, handlers.plugin.key, True)

        await handlers._festival_callback("立冬", "立冬快乐")

        assert pushed == []

    async def test_a_tenant_that_turned_the_plugin_off_is_skipped(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del scope_env
        handlers, pushed = await self._setup(monkeypatch)
        set_global(monkeypatch, handlers.plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, handlers.plugin.key, False)

        await handlers._festival_callback("立冬", "立冬快乐")

        assert pushed == [CHAT2]


# ---------------------------------------------------------------------------
# GitHub 订阅推送
# ---------------------------------------------------------------------------


class TestGithubSubscribers:
    async def test_a_channel_where_the_plugin_is_off_gets_no_event(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from plugins.builtin.github import handlers
        from plugins.builtin.github.models import ChatSubscriptions

        del scope_env
        await make_instance(INST1)
        await make_instance(INST2)
        await make_channel(CHAT1, INST1)
        await make_channel(CHAT2, INST2)
        set_global(monkeypatch, handlers.plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, handlers.plugin.key, False)

        subscriptions = ChatSubscriptions()
        await subscriptions.subscribe_repo("owner/repo", ["push"])
        payload = subscriptions.model_dump_json()

        class _Store:
            async def get(self, *, chat_key: str, store_key: str) -> str:
                del chat_key, store_key
                return payload

        monkeypatch.setattr(handlers, "store", _Store())

        pushed: List[str] = []

        async def push_untrusted(**kwargs: Any) -> None:
            pushed.append(kwargs["chat_key"])

        monkeypatch.setattr(message_service, "push_untrusted_message", push_untrusted)

        await handlers.send_to_subscribers("owner/repo", "push", "事件正文")

        assert pushed == [CHAT2]


# ---------------------------------------------------------------------------
# CC 工作区结果投递
# ---------------------------------------------------------------------------


class TestCcWorkspaceDelivery:
    async def test_a_result_is_kept_in_the_comm_log_but_not_delivered(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from plugins.builtin.cc_workspace import main as cc_main

        del scope_env
        channel = await make_channel(CHAT1, INST1)
        await DBChatChannel.filter(id=channel.id).update(workspace_id=7)
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INST1,
            display_name=INST1,
            status="online",
            provider="remote_onebot",
        )
        set_global(monkeypatch, cc_main.plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, cc_main.plugin.key, False)

        pushed: List[Dict[str, Any]] = []

        async def push_untrusted(**kwargs: Any) -> None:
            pushed.append(kwargs)

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(message_service, "push_untrusted_message", push_untrusted)
        monkeypatch.setattr(cc_main, "_broadcast_cc_status", noop)
        monkeypatch.setattr(cc_main, "_broadcast_system_comm_log", noop)
        monkeypatch.setattr(cc_main.comm_broadcast, "publish", noop)

        workspace = SimpleNamespace(id=7, name="ws")
        item = {"source_chat_key": CHAT1, "result": "后台结果", "id": "result-1", "is_error": False}

        await cc_main._deliver_pending_result(workspace, item, source="watcher")

        assert pushed == []
        assert await DBWorkspaceCommLog.filter(workspace_id=7).count() == 1


# ---------------------------------------------------------------------------
# 心声
# ---------------------------------------------------------------------------


class TestInnerVoice:
    async def test_a_disabled_channel_is_not_armed(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from plugins.builtin import inner_voice as voice

        del scope_env
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, voice.plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, voice.plugin.key, False)
        voice._armed.clear()
        # 布防本身还要过概率、间隔等条件：把它们钉成「一定布防」，否则「没布防」证明不了是开关挡住的
        monkeypatch.setattr(voice, "should_trigger", lambda **_kwargs: True)
        monkeypatch.setattr(voice, "ensure_listener", lambda: False)

        ctx = AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER, channel_type=ChatType.GROUP.value)
        message = ChatMessage.create_empty(CHAT1)
        await voice.on_user_message(ctx, message)

        assert CHAT1 not in voice._armed

        # 对照：同一个频道把插件打开，同样的条件下会布防
        set_scope_override(OverrideScope.INSTANCE, INST1, voice.plugin.key, True)
        await voice.on_user_message(ctx, message)
        assert CHAT1 in voice._armed
        voice._armed.clear()

    async def test_a_disabled_channel_sends_nothing(
        self,
        db,
        scope_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.api import message as message_api
        from plugins.builtin import inner_voice as voice

        del scope_env
        await make_instance(INST1)
        await make_channel(CHAT1, INST1)
        set_global(monkeypatch, voice.plugin.key, True)
        set_scope_override(OverrideScope.INSTANCE, INST1, voice.plugin.key, False)

        sent: List[str] = []

        async def send_text(chat_key: str, _message: str, _ctx: AgentCtx, **_kwargs: Any) -> None:
            sent.append(chat_key)

        monkeypatch.setattr(message_api, "send_text", send_text)
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)

        ctx = AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER)
        await voice.render_and_send(ctx, "心声正文")

        assert sent == []


# ---------------------------------------------------------------------------
# 定时提醒
# ---------------------------------------------------------------------------


class TestTimerScope:
    def test_the_core_constant_matches_the_real_plugin_key(self) -> None:
        from nekro_agent.services.timer.constants import TIMER_PLUGIN_KEY
        from plugins.builtin import timer as timer_plugin_module

        assert timer_plugin_module.plugin.key == TIMER_PLUGIN_KEY

    async def test_a_one_shot_timer_is_skipped_and_does_not_schedule_an_agent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.timer import timer_service as timer_instance

        # `nekro_agent.services.timer` 包把同名实例导到了包属性上，`from ... import timer_service`
        # 拿到的是实例而不是模块；这里按模块名取，才能打桩模块级判据。
        timer_module = importlib.import_module("nekro_agent.services.timer.timer_service")
        service = timer_instance.__class__()
        calls: List[str] = []

        async def effective(_key: str, _chat_key: str) -> bool:
            return False

        async def push(**_kwargs: Any) -> None:
            calls.append("push")

        async def schedule(*_args: Any, **_kwargs: Any) -> None:
            calls.append("schedule")

        monkeypatch.setattr(timer_module, "plugin_effective_in_chat", effective)
        monkeypatch.setattr(timer_module.message_service, "push_untrusted_message", push)
        monkeypatch.setattr(timer_module.message_service, "schedule_agent_task", schedule)

        await service._execute_task(timer_module.TimerTask(CHAT1, 0, "提醒"))  # noqa: SLF001
        await service._execute_task(timer_module.TimerTask(CHAT1, 0, ""))  # noqa: SLF001

        assert calls == []

        # 到点后任务照常从内存队列移除，不会因为跳过而卡住反复触发
        service.tasks[CHAT1] = [timer_module.TimerTask(CHAT1, 0, "提醒")]

        async def stop_loop(_seconds: float) -> None:
            service.running = False

        async def noop_persist() -> None:
            return None

        monkeypatch.setattr(timer_module.asyncio, "sleep", stop_loop)
        monkeypatch.setattr(service, "_persist_tasks", noop_persist)
        service.running = True
        await service._timer_loop()  # noqa: SLF001

        assert CHAT1 not in service.tasks

    async def test_a_skipped_recurring_job_is_not_a_failure(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.timer import recurring_timer_service as recurring_instance

        recurring_module = importlib.import_module("nekro_agent.services.timer.recurring_timer_service")
        job = await DBRecurringTimerJob.create(
            job_id="a1b2",
            chat_key=CHAT1,
            title="每日报表",
            event_desc="提醒正文",
            cron_expr="0 9 * * *",
            timezone="Asia/Shanghai",
            status="active",
        )
        calls: List[str] = []

        async def effective(_key: str, _chat_key: str) -> bool:
            return False

        async def push(**_kwargs: Any) -> None:
            calls.append("push")

        monkeypatch.setattr(recurring_module, "plugin_effective_in_chat", effective)
        monkeypatch.setattr(recurring_module.message_service, "push_untrusted_message", push)

        service = recurring_instance.__class__()
        await service._fire_job(job, fired_at=datetime.now(timezone.utc), is_misfire=False)  # noqa: SLF001

        fresh = await DBRecurringTimerJob.get(job_id="a1b2")
        assert calls == []
        assert fresh.consecutive_failures == 0
        assert fresh.last_error is None
        assert fresh.last_run_at is None
        assert fresh.status == "active"


# ---------------------------------------------------------------------------
# 掷骰与搜索的频道隔离
# ---------------------------------------------------------------------------


class TestDiceAndSearchPerChannel:
    def test_dice_assert_and_lock_are_independent_per_channel(self, scope_env) -> None:
        from plugins.builtin import dice

        del scope_env
        dice._ASSERT_DICE_NUM.clear()
        dice._LOCKED_DICE_NUM.clear()
        dice._ASSERT_DICE_NUM[CHAT1] = 7
        dice._ASSERT_DICE_NUM[CHAT2] = 13
        assert dice._ASSERT_DICE_NUM[CHAT1] == 7
        assert dice._ASSERT_DICE_NUM[CHAT2] == 13
        dice._LOCKED_DICE_NUM[CHAT1] = 20
        assert dice._LOCKED_DICE_NUM[CHAT1] == 20
        assert CHAT2 not in dice._LOCKED_DICE_NUM

    async def test_search_throttle_is_independent_per_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import google_search as search

        monkeypatch.setattr(search, "config", SimpleNamespace(API_KEY="k", CX_KEY="c", MAX_RESULTS=5, THROTTLE_TIME=60))
        search._last_keyword.clear()
        search._last_call_time.clear()

        class _Response:
            def json(self) -> Dict[str, Any]:
                return {"items": [{"title": "t", "snippet": "s", "link": "https://example.invalid"}]}

        class _Client:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def get(self, *_args: Any, **_kwargs: Any) -> _Response:
                return _Response()

        monkeypatch.setattr(search, "AsyncClient", _Client)

        first = await search.google_search(AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER), "同一个词")
        second = await search.google_search(AgentCtx(from_chat_key=CHAT2, adapter_key=ADAPTER), "同一个词")
        blocked = await search.google_search(AgentCtx(from_chat_key=CHAT1, adapter_key=ADAPTER), "同一个词")

        assert not isinstance(first, str)
        assert not isinstance(second, str)
        assert blocked == "[错误] 禁止频繁搜索相同内容，结果无变化"
