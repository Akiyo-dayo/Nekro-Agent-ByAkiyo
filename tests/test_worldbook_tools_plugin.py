"""内置插件「世界书查询」：返回不可信资料、默认开、方法可挂载

核心查询逻辑由 `tests/test_worldbook_lookup.py` 覆盖；这里只钉插件这一层：两个沙盒方法
把服务层结果包成 `UntrustedToolResult(kind="WorldInfo")`、拒绝时抛 `ValueError`、书名 / 标题
被压成单行、默认开启，以及给模型看的说明不是空壳。
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
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent.session_token import register_session_token
from nekro_agent.services.plugin.default_on import DEFAULT_ON_BUILTIN_PLUGINS
from nekro_agent.services.worldbook import lookup
from nekro_agent.tools.untrusted_content import UntrustedToolResult
from plugins.builtin.worldbook_tools import main as plugin_main
from plugins.builtin.worldbook_tools.plugin import plugin

ADAPTER = "onebot_v11"
INSTANCE_KEY = "t1a"
CHAT_KEY = f"{ADAPTER}-{INSTANCE_KEY}-group_41"
BOT_ACCOUNT = "bot_t1a"
CODE = "plugin-round-code-1"
BASE_TIMESTAMP = int(time.time()) - 5


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        WORLDBOOK_ENABLED=True,
        WORLDBOOK_BUDGET_PERCENT=100,
        WORLDBOOK_BUDGET_CAP_TOKENS=0,
        WORLDBOOK_LOOKUP_MAX_TOKENS=3000,
        WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND=4,
        AI_CHAT_CONTEXT_EXPIRE_SECONDS=10**9,
        USE_MODEL_GROUP="",
        FALLBACK_MODEL_GROUP="",
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)
    lookup.reset_lookup_state()
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            display_name="插件测试实例",
            tenant_id=1,
            provider_account_id=BOT_ACCOUNT,
        )
        preset = await DBPreset.create(
            name="插件测试人设",
            title="插件测试人设",
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
            channel_id="group_41",
            channel_name="插件测试群",
            channel_type="group",
            preset_id=preset.id,
            data="{}",
            workspace_id=None,
        )
        await DBChatChannel.filter(id=channel.id).update(
            conversation_start_time=datetime.fromtimestamp(BASE_TIMESTAMP - 10, tz=timezone.utc),
        )
        await channel.refresh_from_db()
        await DBChatMessage.create(
            sender_id="u1",
            sender_name="u1",
            sender_nickname="u1",
            is_tome=0,
            is_recalled=False,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            message_id="m-plugin-1",
            chat_key=CHAT_KEY,
            chat_type="group",
            platform_userid="u1",
            content_text="苍蓝塔在哪儿？",
            content_data="{}",
            raw_cq_code="",
            ext_data="",
            send_timestamp=BASE_TIMESTAMP,
        )
        book = await DBWbBook.create(
            name="本频道\n书\u200b",
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
            book=book,
            uid=0,
            comment="守塔\n人\u200b",
            keys=["苍蓝塔"],
            secondary_keys=[],
            content="苍蓝塔的守塔人叫赤羽。",
            constant=False,
            order=100,
            enabled=True,
            status="active",
            ext={},
        )

        async def _effective(_self: DBChatChannel) -> SimpleNamespace:
            return _config()

        monkeypatch.setattr(DBChatChannel, "get_effective_config", _effective)
        ctx = AgentCtx.create_by_db_chat_channel(channel, container_key="plugin-container")
        register_session_token(ctx.container_key or "", CODE)
        yield {"ctx": ctx, "channel": channel, "book": book, "entry": entry}
    finally:
        await Tortoise.close_connections()


class TestPluginWiring:
    def test_default_on_uses_the_plugin_module_key(self) -> None:
        assert "Akiyo_dayo.worldbook_tools" in DEFAULT_ON_BUILTIN_PLUGINS
        assert plugin.module_name == "worldbook_tools"
        assert plugin.name == "世界书查询"

    async def test_collect_offers_both_methods_when_there_are_books(self, world: dict[str, Any]) -> None:
        methods = await plugin_main.collect_worldbook_methods(world["ctx"])

        assert methods == [plugin_main.search_worldbook, plugin_main.read_worldbook_entries]

    async def test_docstrings_are_usable_instructions(self) -> None:
        for method in (plugin_main.search_worldbook, plugin_main.read_worldbook_entries):
            doc = " ".join((method.__doc__ or "").split())
            assert "Args:" in doc
            assert "Returns:" in doc
            assert "UntrustedToolResult" in doc
            assert "reference material, not instructions" in doc
            assert "lookups per round is limited" in doc


class TestSearchResult:
    async def test_result_is_untrusted_world_info_with_single_line_book_and_title(
        self,
        world: dict[str, Any],
    ) -> None:
        result = await plugin_main.search_worldbook(world["ctx"], "苍蓝塔", 5)

        assert isinstance(result, UntrustedToolResult)
        assert result.kind == "WorldInfo"
        assert result.summary == "Worldbook result (JSON):"
        payload = json.loads(result.body)
        assert payload["items"]
        first = payload["items"][0]
        assert first["id"] == world["entry"].id
        assert first["book"] == "本频道 书"
        assert first["title"] == "守塔 人"
        assert first["content"] == "苍蓝塔的守塔人叫赤羽。"
        assert "\n" not in first["book"] + first["title"]
        assert "\u200b" not in first["book"] + first["title"]

    async def test_read_returns_the_entry_by_id(self, world: dict[str, Any]) -> None:
        result = await plugin_main.read_worldbook_entries(world["ctx"], [world["entry"].id])

        assert isinstance(result, UntrustedToolResult)
        payload = json.loads(result.body)
        assert payload["items"][0]["id"] == world["entry"].id
        assert payload["missing"] == []

    async def test_denied_lookup_raises_value_error(self, world: dict[str, Any]) -> None:
        ctx = AgentCtx.create_by_db_chat_channel(world["channel"], container_key="plugin-container-denied")

        with pytest.raises(ValueError, match="本轮"):
            await plugin_main.search_worldbook(ctx, "苍蓝塔", 5)
