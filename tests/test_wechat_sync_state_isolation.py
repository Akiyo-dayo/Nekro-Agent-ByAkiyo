"""微信 iLink 多实例：绑定态不能污染同步游标

`sync_state_json` 是同步游标 `SyncState` 的序列化载体，读取方按 `extra="forbid"` 严格解析。
以前 `set_session_state(payload=...)` 会把扫码绑定时的 `{bind_session_id, qr_url}` 整体写进去，
于是**绑定一成功**，实例每次启动读游标都抛 `ValidationError`，监控起不来，微信消息完全收不到。

钉住两件事：

1. 写入侧：`set_session_state` 只改 `session_state`，不碰 `sync_state_json`；
2. 读取侧：存量脏数据按空游标继续，日志里不回显原始内容（脏数据里正是扫码绑定凭据）。
"""

import inspect
import json
from types import SimpleNamespace
from typing import List

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.wechat_ilink_multi import bot_connection
from nekro_agent.adapters.wechat_ilink_multi.schemas import SyncState
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.services.adapter_instance_service import adapter_instance_service

SECRET_QR = "https://example.invalid/qr/SECRET-TOKEN"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _seed_instance() -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key="wechat_ilink_multi",
        instance_key="wx1",
        display_name="wx1",
        status="online",
        enabled=True,
        provider="openilink",
        provider_account_id="",
    )


class TestWriteSideKeepsTheCursor:
    def test_set_session_state_no_longer_accepts_a_payload(self) -> None:
        assert "payload" not in inspect.signature(adapter_instance_service.set_session_state).parameters

    async def test_existing_cursor_survives_a_bind_state_change(self, db) -> None:
        instance = await _seed_instance()
        cursor = SyncState(cursor="c-1", sequence=3).model_dump_json()
        await DBAdapterInstanceSession.create(instance_id=instance.id, session_state="pending", sync_state_json=cursor)

        await adapter_instance_service.set_session_state(instance, "bound")

        session = await DBAdapterInstanceSession.get(instance_id=instance.id)
        assert session.session_state == "bound"
        assert session.sync_state_json == cursor
        assert SyncState.model_validate_json(session.sync_state_json).cursor == "c-1"

    async def test_new_session_row_starts_with_an_empty_cursor(self, db) -> None:
        instance = await _seed_instance()
        session = await adapter_instance_service.set_session_state(instance, "pending")
        assert session.sync_state_json == ""


class TestReadSideToleratesDirtyData:
    @staticmethod
    def _owner(raw: str) -> SimpleNamespace:
        return SimpleNamespace(session=SimpleNamespace(sync_state_json=raw), instance_key="wx1")

    def test_dirty_bind_payload_falls_back_to_an_empty_cursor_without_leaking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: List[str] = []
        monkeypatch.setattr(bot_connection.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))
        dirty = json.dumps({"bind_session_id": "bind-123", "qr_url": SECRET_QR})

        state = bot_connection.BotConnection._load_sync_state(self._owner(dirty))  # type: ignore[arg-type]

        assert state == SyncState()
        assert len(warnings) == 1
        assert SECRET_QR not in warnings[0] and "bind-123" not in warnings[0]
        assert "qr_url" in warnings[0], "告警里应说明是哪个字段出了问题"

    def test_valid_cursor_still_loads(self) -> None:
        raw = SyncState(cursor="c-9").model_dump_json()
        state = bot_connection.BotConnection._load_sync_state(self._owner(raw))  # type: ignore[arg-type]
        assert state.cursor == "c-9"
