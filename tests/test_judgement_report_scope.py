"""禁言报告频道的租户归属

`ADMIN_CHAT_KEY` 是插件配置，租户能在自己实例上覆盖成任意频道。发送前不判归属，
租户就能让自己的 Bot 每次禁言都往别的租户群里发一条带 QQ 号与理由的报告；平台方的
全局默认值也会让租户群的报告进平台方的管理频道。本模块钉住：不满足就跳过并记
WARNING，禁言本身照常执行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.logger import logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_ctx import AgentCtx
from plugins.builtin import judgement

TENANT_A = 41
TENANT_B = 42
ADAPTER = "onebot_v11"
OWN_CHANNEL = "group_10001"
TARGET_CHANNEL = "group_20002"
OWN_CHAT = f"{ADAPTER}-scope_a-{OWN_CHANNEL}"
FOREIGN_CHAT = f"{ADAPTER}-scope_b-{TARGET_CHANNEL}"
SAME_TENANT_CHAT = f"{ADAPTER}-scope_a2-{TARGET_CHANNEL}"


class FakeBot:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def set_group_ban(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def add_instance(instance_key: str, tenant_id: Optional[int]) -> None:
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


async def add_channel(instance_key: str, chat_key: str, channel_id: str) -> None:
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        chat_key=chat_key,
        channel_type="group",
        data="{}",
    )


async def init_db() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()


@pytest.fixture
async def multitenant_db():
    await init_db()
    try:
        await add_instance("scope_a", TENANT_A)
        await add_instance("scope_b", TENANT_B)
        await add_instance("scope_a2", TENANT_A)
        await add_channel("scope_a", OWN_CHAT, OWN_CHANNEL)
        await add_channel("scope_b", FOREIGN_CHAT, TARGET_CHANNEL)
        await add_channel("scope_a2", SAME_TENANT_CHAT, TARGET_CHANNEL)
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def single_tenant_db():
    await init_db()
    try:
        await add_instance("scope_a", None)
        await add_instance("scope_b", None)
        await add_channel("scope_a", OWN_CHAT, OWN_CHANNEL)
        await add_channel("scope_b", FOREIGN_CHAT, TARGET_CHANNEL)
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """把禁言动作、报告发送与配置都换成可控替身"""
    bot = FakeBot()
    sent: List[tuple[str, str]] = []

    async def _fake_get_bot(_chat_key: str) -> FakeBot:
        return bot

    async def _fake_send_text(chat_key: str, message_text: str, _ctx: AgentCtx) -> None:
        sent.append((chat_key, message_text))

    global_config = judgement.plugin.get_global_config()
    monkeypatch.setattr(judgement, "get_bot_by_chat_key", _fake_get_bot)
    monkeypatch.setattr(judgement.message, "send_text", _fake_send_text)
    monkeypatch.setattr(global_config, "ENABLE_ADMIN_REPORT", True)
    monkeypatch.setattr(global_config, "ADMIN_CHAT_KEY", "")
    return bot, sent, global_config


def ctx_of(chat_key: str, channel_id: str) -> AgentCtx:
    return AgentCtx(from_chat_key=chat_key, container_key="judgement-test", channel_id=channel_id)


def capture_warnings() -> tuple[List[str], int]:
    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    return captured, sink_id


class TestCrossTenantReport:
    @pytest.mark.asyncio
    async def test_foreign_report_channel_is_skipped_but_mute_proceeds(self, multitenant_db, patched):
        bot, sent, global_config = patched
        global_config.ADMIN_CHAT_KEY = FOREIGN_CHAT
        warnings, sink_id = capture_warnings()

        try:
            result = await judgement.mute_user(ctx_of(OWN_CHAT, OWN_CHANNEL), OWN_CHAT, "10001", 60, "刷屏")
        finally:
            logger.remove(sink_id)

        assert sent == []
        assert len(bot.calls) == 1
        assert bot.calls[0]["duration"] == 60
        assert "已禁言" in result
        assert "报告" not in result
        assert FOREIGN_CHAT not in result
        assert any(
            "禁言报告频道与当前会话不在同一租户" in line and OWN_CHAT in line and FOREIGN_CHAT in line
            for line in warnings
        )

    @pytest.mark.asyncio
    async def test_same_tenant_report_is_sent(self, multitenant_db, patched):
        _bot, sent, global_config = patched
        global_config.ADMIN_CHAT_KEY = SAME_TENANT_CHAT

        result = await judgement.mute_user(ctx_of(OWN_CHAT, OWN_CHANNEL), OWN_CHAT, "10001", 60, "刷屏")

        assert "已禁言" in result
        assert len(sent) == 1
        assert sent[0][0] == SAME_TENANT_CHAT

    @pytest.mark.asyncio
    async def test_single_tenant_deployment_sends_as_before(self, single_tenant_db, patched):
        _bot, sent, global_config = patched
        global_config.ADMIN_CHAT_KEY = FOREIGN_CHAT

        result = await judgement.mute_user(ctx_of(OWN_CHAT, OWN_CHANNEL), OWN_CHAT, "10001", 60, "刷屏")

        assert "已禁言" in result
        assert len(sent) == 1
        assert sent[0][0] == FOREIGN_CHAT

    @pytest.mark.asyncio
    async def test_empty_admin_chat_key_still_sends_nothing(self, multitenant_db, patched):
        _bot, sent, global_config = patched
        global_config.ADMIN_CHAT_KEY = ""

        result = await judgement.mute_user(ctx_of(OWN_CHAT, OWN_CHANNEL), OWN_CHAT, "10001", 60, "刷屏")

        assert "已禁言" in result
        assert sent == []
