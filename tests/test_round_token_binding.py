"""正式一轮里插件拿得到本轮令牌，提示词预览拿不到

正式一轮的上下文由 `AgentCtx.create_by_chat_key` / `create_by_db_chat_channel` 建出来（消息服务与
`run_agent` 都是这么建的），`container_key` 是 `None`，按容器键查不到令牌；令牌由 `run_agent` 绑在
上下文上（`bind_round_token`），收尾时解绑。以前没有这一步，插件的方法清单钩子与提示词注入拿到的
令牌都是空串：世界书查询的两个方法在正式一轮里从不出现，角色状态块一直是空的；只有手工给上下文
塞了 `container_key` 的用例才看得到它们。

这里的上下文一律照消息服务的建法、不塞 `container_key`，钉住：

1. 令牌接口本身：绑定优先、只解绑自己那一份、没绑时回落按容器键查、不进序列化与 `repr`；
2. `run_agent`：交给提示词组装的就是绑好令牌的那个上下文，`container_key` 仍是 `None`，收尾后解绑；
3. 世界书查询：绑好令牌的上下文经插件提示词渲染与沙盒方法清单都拿得到两个方法；同一频道正有
   一轮在跑时，新建的（预览用的）上下文拿不到令牌、拿不到方法；
4. 角色状态：绑好令牌的上下文经插件提示词渲染注入了状态块；预览拿不到令牌，不建模板、不建状态、
   不清开场白初值；
5. 查询结果落库的那一份只有编号与计数。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_state_template import DBStateTemplate
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.affection.suppression import lift_narrative_suppression
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.session_token import clear_session_token, register_session_token
from nekro_agent.services.agent.templates.plugin import render_plugins_prompt
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.sandbox.runner import build_container_key
from nekro_agent.services.tavern.greeting import GREETING_INITVAR_KEY
from nekro_agent.services.worldbook import lookup
from plugins.builtin.state_table import main as state_main  # noqa: F401  注册状态块的注入方法
from plugins.builtin.state_table import session, updater
from plugins.builtin.state_table.plugin import plugin as state_plugin
from plugins.builtin.worldbook_tools import main as wb_main
from plugins.builtin.worldbook_tools.plugin import plugin as wb_plugin

ADAPTER = "onebot_v11"
INSTANCE_KEY = "rt1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE_KEY}-group_52"
CODE = "round-token-code-1"
FEATURE_BOOK = "书名特征词青岚"
FEATURE_TITLE = "标题特征词白鹭"
FEATURE_BODY = "正文特征词赤羽"

INITVAR = """
当前时间: 早上
所在地点: 码头
"""


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        WORLDBOOK_ENABLED=True,
        WORLDBOOK_BUDGET_PERCENT=100,
        WORLDBOOK_BUDGET_CAP_TOKENS=0,
        WORLDBOOK_LOOKUP_MAX_TOKENS=3000,
        WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND=4,
        AI_CHAT_CONTEXT_EXPIRE_SECONDS=10**9,
        USE_MODEL_GROUP="default",
        FALLBACK_MODEL_GROUP="",
        MODEL_GROUPS={},
        AFFECTION_ENABLE_SYSTEM=False,
        STATE_PRESENT_WINDOW_MINUTES=30,
        STATE_PROMPT_MAX_TOKENS=1200,
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)
    lookup.reset_lookup_state()
    session.rounds.clear()
    updater.reset_for_tests()
    prompt_rounds.reset_prompt_rounds()
    lift_narrative_suppression(CHAT_KEY)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            display_name="令牌测试实例",
            tenant_id=1,
            provider_account_id="bot_rt1",
        )
        preset = await DBPreset.create(
            name="令牌测试人设",
            title="令牌测试人设",
            avatar="",
            content="人设正文",
            description="",
            tags="",
            author="test",
            owner_instance_key=INSTANCE_KEY,
        )
        channel = await DBChatChannel.create(
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            channel_id="group_52",
            channel_name="令牌测试群",
            channel_type="group",
            preset_id=preset.id,
            data=json.dumps({GREETING_INITVAR_KEY: {"preset_id": preset.id, "text": "当前时间: 深夜"}}),
            workspace_id=None,
        )
        await DBChatChannel.filter(id=channel.id).update(
            conversation_start_time=datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc),
        )
        await DBChatMessage.create(
            sender_id="u1",
            sender_name="u1",
            sender_nickname="u1",
            is_tome=0,
            is_recalled=False,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            message_id="m-token-1",
            chat_key=CHAT_KEY,
            chat_type="group",
            platform_userid="u1",
            content_text="苍蓝塔在哪儿？",
            content_data="[]",
            raw_cq_code="",
            ext_data="",
            send_timestamp=int(time.time()) - 5,
        )
        domain_book = await DBWbBook.create(
            name=FEATURE_BOOK,
            description="",
            scope_kind="domain",
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            preset_id=0,
            chat_key="",
            person_account="",
            enabled=True,
            scan_depth=4,
            budget_tokens=0,
            inject_mode="both",
            active_scene="both",
        )
        entry = await DBWbEntry.create(
            book=domain_book,
            uid=0,
            comment=FEATURE_TITLE,
            keys=["苍蓝塔"],
            secondary_keys=[],
            content=f"苍蓝塔的守塔人叫{FEATURE_BODY}。",
            constant=False,
            order=100,
            enabled=True,
            status="active",
            ext={},
        )
        preset_book = await DBWbBook.create(
            name="卡内书",
            description="",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            preset_id=preset.id,
            chat_key="",
            person_account="",
            enabled=True,
            scan_depth=4,
            budget_tokens=0,
        )
        await DBWbEntry.create(
            book=preset_book,
            uid=1,
            comment="[initvar]初始变量",
            keys=[],
            secondary_keys=[],
            content=INITVAR,
            constant=False,
            order=100,
            enabled=True,
            trusted=True,
            status="active",
            ext={},
        )

        async def _effective(_self: DBChatChannel) -> SimpleNamespace:
            return _config()

        monkeypatch.setattr(DBChatChannel, "get_effective_config", _effective)
        yield SimpleNamespace(channel=channel, preset=preset, entry=entry)
    finally:
        clear_session_token(build_container_key(CHAT_KEY))
        session.rounds.clear()
        lookup.reset_lookup_state()
        await Tortoise.close_connections()


async def _round_ctx(code: str = CODE) -> AgentCtx:
    """照 `run_agent` 的做法：按 chat_key 建上下文，登记服务端令牌，再绑到上下文上"""
    ctx = await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY)
    register_session_token(build_container_key(CHAT_KEY), code)
    run_agent_module._bind_round_token(ctx, code)  # noqa: SLF001
    return ctx


async def _state_rows() -> int:
    return (
        await DBStateTemplate.all().count()
        + await DBStateChannel.all().count()
        + await DBStatePerson.all().count()
        + await DBStateSnapshot.all().count()
    )


def _method_names(rendered: str) -> set[str]:
    return {name for name in ("search_worldbook", "read_worldbook_entries") if name in rendered}


# ---------------------------------------------------------------------------
# 1. 令牌接口
# ---------------------------------------------------------------------------


class TestAgentCtxToken:
    def test_bound_token_wins_and_unbind_only_clears_its_own(self) -> None:
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        assert ctx.one_time_code == ""

        ctx.bind_round_token("code-a")
        assert ctx.one_time_code == "code-a"
        ctx.unbind_round_token("code-b")
        assert ctx.one_time_code == "code-a", "别的轮次的令牌解不开这一份"
        ctx.unbind_round_token("code-a")
        assert ctx.one_time_code == ""

        ctx.bind_round_token("")
        assert ctx.one_time_code == "", "空令牌不绑定"

    def test_unbound_ctx_falls_back_to_the_container_key(self) -> None:
        register_session_token("token-container-x", "code-from-container")
        try:
            assert AgentCtx(container_key="token-container-x").one_time_code == "code-from-container"
            bound = AgentCtx(container_key="token-container-x")
            bound.bind_round_token("code-bound")
            assert bound.one_time_code == "code-bound"
        finally:
            clear_session_token("token-container-x")

    def test_the_token_never_shows_up_in_serialisation_or_repr(self) -> None:
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        ctx.bind_round_token("secret-round-code-123")

        assert "secret-round-code-123" not in json.dumps(ctx.model_dump(), ensure_ascii=False, default=str)
        assert "secret-round-code-123" not in ctx.model_dump_json()
        assert "secret-round-code-123" not in repr(ctx)
        assert "secret-round-code-123" not in str(ctx)
        assert "_round_token" not in ctx.model_dump()

    def test_a_round_finished_copy_keeps_the_token(self) -> None:
        """回合收尾回调拿的是 `model_copy()`：解绑原件之后，副本仍带着本轮令牌"""
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        ctx.bind_round_token("code-copy")
        copied = ctx.model_copy()
        ctx.unbind_round_token("code-copy")

        assert copied.one_time_code == "code-copy"
        assert ctx.one_time_code == ""


# ---------------------------------------------------------------------------
# 2. run_agent 的接线
# ---------------------------------------------------------------------------


class _Stop(Exception):
    pass


class TestRunAgentBindsTheRound:
    async def test_the_prompt_is_built_with_the_bound_ctx_and_it_is_unbound_afterwards(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def spy_build_round_prompt(**kwargs: Any) -> Any:
            ctx = kwargs["ctx"]
            seen["ctx"] = ctx
            seen["code"] = ctx.one_time_code
            seen["container_key"] = ctx.container_key
            # 插件的方法清单钩子在提示词组装里拿的就是这个上下文
            seen["methods"] = await wb_main.collect_worldbook_methods(ctx)
            raise _Stop

        async def nothing(*_args: Any, **_kwargs: Any) -> Any:
            return 0

        monkeypatch.setattr(run_agent_module, "build_round_prompt", spy_build_round_prompt)
        monkeypatch.setattr(run_agent_module, "open_ops_round", nothing)
        monkeypatch.setattr(run_agent_module, "_channel_message_marker", nothing)
        monkeypatch.setattr(run_agent_module, "_record_round_reply", nothing)
        ctx = await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY)

        with pytest.raises(_Stop):
            await run_agent_module._run_agent_with_affection_suppression(  # noqa: SLF001
                chat_key=CHAT_KEY,
                chat_message=None,
                ctx=ctx,
                db_chat_channel=world.channel,
                one_time_code=CODE,
            )

        assert seen["ctx"] is ctx
        assert seen["code"] == CODE
        assert seen["container_key"] is None, "不靠设 container_key 取令牌"
        assert seen["methods"] == [wb_main.search_worldbook, wb_main.read_worldbook_entries]
        assert ctx.one_time_code == "", "收尾时解绑"


# ---------------------------------------------------------------------------
# 3. 世界书查询
# ---------------------------------------------------------------------------


class TestWorldbookToolsWithTheRoundCtx:
    async def test_bound_ctx_gets_both_methods_in_the_prompt_and_the_sandbox(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = await _round_ctx()
        assert ctx.container_key is None

        rendered = await render_plugins_prompt([wb_plugin], ctx, activation_enabled=False, one_time_code=CODE)
        assert _method_names(rendered.system_prompt) == {"search_worldbook", "read_worldbook_entries"}

        async def only_worldbook(**_kwargs: Any) -> list[Any]:
            return [wb_plugin]

        import nekro_agent.services.plugin.scope as scope_module

        monkeypatch.setattr(scope_module, "resolve_active_plugins", only_worldbook)
        methods = await plugin_collector.get_all_sandbox_methods(ctx)
        assert {method.func.__name__ for method in methods} >= {"search_worldbook", "read_worldbook_entries"}

    async def test_a_fresh_ctx_gets_neither_the_token_nor_the_methods_while_a_round_is_live(
        self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _round_ctx()  # 同一频道正有一轮在跑：服务端登记着活的令牌
        preview_ctx = await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY)

        assert preview_ctx.one_time_code == "", "不按 chat_key 反查当轮令牌"
        assert await wb_main.collect_worldbook_methods(preview_ctx) == []
        rendered = await render_plugins_prompt(
            [wb_plugin], preview_ctx, activation_enabled=False, call_inject=False, one_time_code=""
        )
        assert _method_names(rendered.system_prompt) == set()

        async def only_worldbook(**_kwargs: Any) -> list[Any]:
            return [wb_plugin]

        import nekro_agent.services.plugin.scope as scope_module

        monkeypatch.setattr(scope_module, "resolve_active_plugins", only_worldbook)
        methods = await plugin_collector.get_all_sandbox_methods(preview_ctx)
        assert not {method.func.__name__ for method in methods} & {"search_worldbook", "read_worldbook_entries"}


# ---------------------------------------------------------------------------
# 4. 角色状态
# ---------------------------------------------------------------------------


class TestStateBlockWithTheRoundCtx:
    async def test_bound_ctx_gets_the_state_block_through_plugin_rendering(self, world: SimpleNamespace) -> None:
        ctx = await _round_ctx()
        prompt_rounds.begin(CODE, world.channel)

        rendered = await render_plugins_prompt([state_plugin], ctx, activation_enabled=False, one_time_code=CODE)

        blocks = "\n".join(text for _depth, text in rendered.depth_blocks)
        assert "码头" in blocks or "深夜" in blocks, rendered

    async def test_preview_gets_no_token_and_writes_nothing(self, world: SimpleNamespace) -> None:
        await _round_ctx()  # 同一频道正有一轮在跑
        preview_ctx = await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY)
        before = await _state_rows()

        rendered = await render_plugins_prompt(
            [wb_plugin, state_plugin], preview_ctx, activation_enabled=False, call_inject=False, one_time_code=""
        )
        # 预览不调注入方法；就算有别的路径直接调了状态块，没有令牌也什么都不做
        assert await state_main.state_block(preview_ctx) == ""

        assert rendered.depth_blocks == []
        assert _method_names(rendered.system_prompt) == set()
        assert await _state_rows() == before == 0
        await world.channel.refresh_from_db()
        assert GREETING_INITVAR_KEY in world.channel._load_data(), "开场白初值没被清掉"  # noqa: SLF001


# ---------------------------------------------------------------------------
# 5. 查询结果落库的那一份
# ---------------------------------------------------------------------------


class TestWorldbookResultHistory:
    async def test_history_body_only_has_ids_and_counts(self, world: SimpleNamespace) -> None:
        ctx = await _round_ctx()

        result = await wb_main.search_worldbook(ctx, "苍蓝塔")
        missing = await wb_main.read_worldbook_entries(ctx, [world.entry.id, 987654])

        for item in (result, missing):
            assert item.history_body is not None
            stored = json.loads(item.history_body)
            assert set(stored) == {"note", "entry_ids", "missing_count", "truncated"}
            for feature in (FEATURE_BOOK, FEATURE_TITLE, FEATURE_BODY):
                assert feature not in item.history_body
                assert feature in item.body
            assert item.stored_body == item.history_body
        assert json.loads(result.history_body or "")["entry_ids"] == [world.entry.id]
        assert json.loads(missing.history_body or "")["missing_count"] == 1
