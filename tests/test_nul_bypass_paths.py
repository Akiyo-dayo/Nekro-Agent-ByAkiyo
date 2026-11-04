"""绕过 ORM 保存信号的几条写入路径：写库前自己去掉 NUL

`chat_message` / `user` / `chat_channel` 三张表有 `pre_save` 兜底（`nekro_agent/models/nul_guard.py`），
但 `filter().update()` 与 `bulk_create` 不走保存信号，其他表也不在兜底范围里。下面几处会把协议端或用户
提供的文本写进库，PostgreSQL 的 text 列不收 0x00，所以由调用方先清理。SQLite 能存 NUL，
这里断言的是「落库的值已经清理过」，不依赖数据库报错。
"""

from types import SimpleNamespace

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.wxwork.user_resolver import WxWorkUserResolver
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.plugin.base import PluginStore

NUL = "\x00"
MIXED = f"a{NUL}b"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _new_message() -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id="u1",
        sender_name="sender",
        sender_nickname="nick",
        is_tome=0,
        is_recalled=False,
        adapter_key="wxwork",
        instance_key="",
        message_id="m1",
        chat_key="wxwork-group-1",
        chat_type="group",
        platform_userid="u1",
        content_text="text",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000_000,
    )


class TestPathsThatBypassSaveSignals:
    async def test_wxwork_backfill_uses_clean_name(self, db) -> None:
        await _new_message()
        resolver = WxWorkUserResolver(SimpleNamespace(key="wxwork"))

        await resolver._backfill_chat_message_names(user_id="u1", user_name=MIXED)

        row = await DBChatMessage.get(platform_userid="u1")
        assert (row.sender_name, row.sender_nickname) == ("ab", "ab")

    async def test_plugin_store_create_strips_nul(self, db) -> None:
        store = PluginStore(SimpleNamespace(key="nul_test_plugin"))

        assert await store.set(chat_key="k", store_key="s", value=MIXED) == 0

        row = await DBPluginData.get(plugin_key="nul_test_plugin", data_key="s")
        assert row.data_value == "ab"

    async def test_plugin_store_update_strips_nul(self, db) -> None:
        store = PluginStore(SimpleNamespace(key="nul_test_plugin"))
        await store.set(chat_key="k", store_key="s", value="first")

        # 第二次写入走的是 `filter().update()` 那条分支
        assert await store.set(chat_key="k", store_key="s", value=MIXED) == 1

        row = await DBPluginData.get(plugin_key="nul_test_plugin", data_key="s")
        assert row.data_value == "ab"

    async def test_runtime_status_error_text_strips_nul(self, db) -> None:
        instance = await DBAdapterInstance.create(adapter_key="wxwork", instance_key="wx1", status="pending")

        await adapter_instance_service.set_runtime_status(instance, "error", MIXED)

        row = await DBAdapterInstance.get(id=instance.id)
        assert (row.status, row.last_error) == ("error", "ab")
