"""角色状态在正式一轮里拿得到本轮令牌：注入、变量、顺手更新、兼容解析、收尾提交一路走通

上下文一律照正式运行的建法，不手工塞 `container_key`：

- `run_agent` 的上下文：`AgentCtx.create_by_chat_key` 建出来，`run_agent` 绑上本轮令牌；回合收尾的回调拿的是
  `schedule_round_finished` 复制出来的副本，原件随后解绑；
- 沙盒方法与沙盒里发消息的上下文：RPC 按容器键建（`container_key` 是容器键），令牌按容器键查。

钉住：

1. 正式一轮：状态块注入了；变量提供者给了值；顺手更新与发送路径交来的变量块都按本轮令牌进了缓冲；
   收尾时两种写入都提交了；
2. 同一频道正有一轮在跑时，新建的上下文（预览一类）拿不到令牌：顺手更新不记、变量块不收，
   用它收尾也不会提交正在跑的那一轮。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.affection.suppression import lift_narrative_suppression
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.session_token import clear_session_token, register_session_token
from nekro_agent.services.agent.templates.plugin import render_plugins_prompt
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.plugin import collector as collector_module
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.sandbox.runner import build_container_key
from nekro_agent.services.tavern import variable_blocks
from nekro_agent.services.worldbook import variables as wb_variables
from plugins.builtin.state_table import inline, session, updater
from plugins.builtin.state_table import main as state_main  # noqa: F401  登记注入、变量来源与收尾回调
from plugins.builtin.state_table.plugin import StateTableConfig
from plugins.builtin.state_table.plugin import plugin as state_plugin

ADAPTER = "onebot_v11"
INSTANCE_KEY = "rt2"
CHAT_KEY = f"{ADAPTER}-{INSTANCE_KEY}-private_u1"
CODE = "state-round-token-1"

INITVAR = """
当前时间: 早上
所在地点: 码头
"""


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        WORLDBOOK_ENABLED=True,
        AI_CHAT_CONTEXT_EXPIRE_SECONDS=10**9,
        USE_MODEL_GROUP="default",
        MODEL_GROUPS={},
        AFFECTION_ENABLE_SYSTEM=False,
        STATE_PRESENT_WINDOW_MINUTES=30,
        STATE_PROMPT_MAX_TOKENS=1200,
        STATE_COMPAT_PARSE="private",
        STATE_IMAGE_SEND_MODE="command",
        MEMORY_CONSOLIDATION_MSG_THRESHOLD=50,
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)
    # 插件在这个实例上真的生效（判生效走核心的真实判定，不打桩）
    monkeypatch.setattr(config, "PLUGIN_ENABLED", [*config.PLUGIN_ENABLED, state_plugin.key])
    # 实例开了顺手更新；其余照出厂值
    monkeypatch.setattr(updater, "scoped_config", lambda _key: StateTableConfig(INLINE_UPDATE_ENABLED=True))
    scheduled: list[str] = []
    monkeypatch.setattr(updater, "schedule", scheduled.append)
    monkeypatch.setattr(plugin_collector, "loaded_plugins", {state_plugin.key: state_plugin})
    # 每条用例开始时接收方是空的（见 conftest）：照插件自己的方式登记
    variable_blocks.register_variable_block_sink(
        state_plugin.key, inline.on_variable_blocks, active=lambda: state_plugin.is_enabled
    )
    session.rounds.clear()
    updater.reset_for_tests()
    inline.reset_for_tests()
    prompt_rounds.reset_prompt_rounds()
    lift_narrative_suppression(CHAT_KEY)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            display_name="状态令牌测试实例",
            tenant_id=1,
            provider_account_id="bot_rt2",
        )
        preset = await DBPreset.create(
            name="阿青",
            title="阿青",
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
            channel_id="private_u1",
            channel_name="小林",
            channel_type="private",
            preset_id=preset.id,
            data="{}",
            workspace_id=None,
        )
        await DBChatChannel.filter(id=channel.id).update(
            conversation_start_time=datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc),
        )
        await channel.refresh_from_db()
        await DBChatMessage.create(
            sender_id="u1",
            sender_name="u1",
            sender_nickname="小林",
            is_tome=1,
            is_recalled=False,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            message_id="m-state-token-1",
            chat_key=CHAT_KEY,
            chat_type="private",
            platform_userid="u1",
            content_text="天黑了，我们去屋顶吧",
            content_data="[]",
            raw_cq_code="",
            ext_data="",
            send_timestamp=int(time.time()) - 5,
        )
        book = await DBWbBook.create(
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
            book=book,
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
        yield SimpleNamespace(channel=channel, preset=preset, scheduled=scheduled)
    finally:
        clear_session_token(build_container_key(CHAT_KEY))
        session.rounds.clear()
        inline.reset_for_tests()
        await Tortoise.close_connections()


async def _round_ctx() -> AgentCtx:
    """照 `run_agent`：按 chat_key 建上下文，登记服务端令牌，再绑到上下文上"""
    ctx = await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY)
    register_session_token(build_container_key(CHAT_KEY), CODE)
    run_agent_module._bind_round_token(ctx, CODE)  # noqa: SLF001
    return ctx


async def _sandbox_ctx() -> AgentCtx:
    """照沙盒回调（RPC）的建法：带容器键"""
    return await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY, container_key=build_container_key(CHAT_KEY))


async def _finish(ctx: AgentCtx, outcome: str) -> None:
    """照 `run_agent` 的收尾：排收尾回调（拿副本），再解绑原件，等回调跑完"""
    plugin_collector.schedule_round_finished(ctx, outcome)
    run_agent_module._unbind_round_token(ctx, CODE)  # noqa: SLF001
    await asyncio.gather(*list(collector_module._ROUND_FINISHED_TASKS))  # noqa: SLF001


async def _state() -> dict[str, object]:
    return dict((await DBStateChannel.get(chat_key=CHAT_KEY)).data)


class TestAFormalRound:
    async def test_injection_variables_inline_compat_and_commit(self, world: SimpleNamespace) -> None:
        ctx = await _round_ctx()
        assert ctx.container_key is None
        prompt_rounds.begin(CODE, world.channel)

        # 1. 注入：状态块进了提示词（这一步顺带按书初始化了模板与状态）
        rendered = await render_plugins_prompt([state_plugin], ctx, activation_enabled=False, one_time_code=CODE)
        blocks = "\n".join(text for _depth, text in rendered.depth_blocks)
        assert "码头" in blocks, rendered

        # 2. 变量提供者给了值
        provider = await wb_variables.provider_for_round(
            wb_variables.RoundVariableContext(
                one_time_code=CODE,
                channel=world.channel,
                chat_key=CHAT_KEY,
                preset_id=None,
                is_private=True,
                trigger_person_uid="u1",
                config=_config(),
                dry_run=False,
            )
        )
        assert provider.get("cache", "stat_data.当前时间") == "早上"

        # 3. 沙盒里调顺手更新、沙盒里发的消息带着变量块：都按本轮令牌进缓冲
        sandbox_ctx = await _sandbox_ctx()
        assert sandbox_ctx.one_time_code == CODE
        reply = await inline.update_state(sandbox_ctx, [{"op": "replace", "path": "/当前时间", "value": "傍晚"}])
        assert "记下 1 处" in reply
        await universal_chat_service._deliver_blocks(sandbox_ctx, CHAT_KEY, ["_.set('所在地点', '屋顶');"])  # noqa: SLF001

        # 4. 收尾：回调拿到的副本带着令牌，两种写入都提交，这一轮的装载缓存弹掉
        assert session.rounds.get(CODE) is not None
        await _finish(ctx, "ok")

        assert ctx.one_time_code == ""
        assert session.rounds.get(CODE) is None
        assert await _state() == {"当前时间": "傍晚", "所在地点": "屋顶"}
        sources = set(await DBStateSnapshot.filter(target_kind="channel").values_list("source", flat=True))
        assert {"inline", "compat"} <= sources
        assert world.scheduled == []  # 这一轮已经顺手改过，不再排分步更新
        assert inline._buffers == {}  # noqa: SLF001


class TestWithoutTheRoundToken:
    async def test_a_fresh_ctx_records_nothing_and_does_not_finish_the_live_round(self, world: SimpleNamespace) -> None:
        ctx = await _round_ctx()  # 同一频道正有一轮在跑
        prompt_rounds.begin(CODE, world.channel)
        await render_plugins_prompt([state_plugin], ctx, activation_enabled=False, one_time_code=CODE)
        sandbox_ctx = await _sandbox_ctx()
        await inline.update_state(sandbox_ctx, [{"op": "replace", "path": "/当前时间", "value": "傍晚"}])

        fresh = await AgentCtx.create_by_chat_key(chat_key=CHAT_KEY)
        assert fresh.one_time_code == "", "不按 chat_key 反查当轮令牌"

        reply = await inline.update_state(fresh, [{"op": "replace", "path": "/所在地点", "value": "桥上"}])
        await universal_chat_service._deliver_blocks(fresh, CHAT_KEY, ["_.set('所在地点', '桥上');"])  # noqa: SLF001
        plugin_collector.schedule_round_finished(fresh, "ok")
        await asyncio.gather(*list(collector_module._ROUND_FINISHED_TASKS))  # noqa: SLF001

        assert reply == "这一轮不能更新角色状态，没有记下。"
        assert await _state() == {"当前时间": "早上", "所在地点": "码头"}  # 正在跑的那一轮没被它提交
        buffer = inline.take(CODE, CHAT_KEY)
        assert buffer is not None and len(buffer.inline_ops) == 1 and buffer.blocks == []
        assert set(inline._buffers) == set()  # noqa: SLF001
