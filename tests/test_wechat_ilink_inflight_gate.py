"""微信 iLink 多实例：停止 / 删除时间窗里到达的在途消息

BotConnection 的长轮询在停止流程走完之前还会交付消息。`_instance_still_active` 在送进消息入口之前复核：
连接已在停止中直接拒绝（不必查库）；实例不在或已软删时拒绝，并让残留连接自行退出；
实例被停用时只拒绝消息、**连接保持**——停用是「连着但不理」，重新启用要立刻恢复。
"""

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from nekro_agent.adapters.wechat_ilink_multi import bot_connection


def _owner(stopped: bool = False) -> SimpleNamespace:
    event = asyncio.Event()
    if stopped:
        event.set()
    return SimpleNamespace(_stopped=event, adapter=SimpleNamespace(key="wechat_ilink_multi"), instance_key="wx1")


def _fake_lookup(monkeypatch: pytest.MonkeyPatch, instance: Optional[Any], calls: List[str]) -> None:
    async def get_instance(adapter_key: str, instance_key: str) -> Optional[Any]:
        calls.append(f"{adapter_key}/{instance_key}")
        return instance

    monkeypatch.setattr(bot_connection.adapter_instance_service, "get_instance", get_instance)


async def test_stopping_connection_rejects_without_touching_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    _fake_lookup(monkeypatch, SimpleNamespace(status="online", enabled=True), calls)

    assert await bot_connection.BotConnection._instance_still_active(_owner(stopped=True)) is False  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.parametrize(
    "instance",
    [None, SimpleNamespace(status="deleted", enabled=True)],
    ids=["missing", "deleted"],
)
async def test_gone_instance_rejects_and_stops_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    instance: Optional[Any],
) -> None:
    calls: List[str] = []
    _fake_lookup(monkeypatch, instance, calls)
    owner = _owner()

    assert await bot_connection.BotConnection._instance_still_active(owner) is False  # type: ignore[arg-type]
    assert owner._stopped.is_set(), "残留连接应当自行退出长轮询"
    assert calls == ["wechat_ilink_multi/wx1"]


async def test_disabled_instance_drops_the_message_but_keeps_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    _fake_lookup(monkeypatch, SimpleNamespace(status="online", enabled=False), calls)
    owner = _owner()

    assert await bot_connection.BotConnection._instance_still_active(owner) is False  # type: ignore[arg-type]
    assert not owner._stopped.is_set(), "停用不能停掉连接：重新启用后没有人会再把它拉起来"


async def test_active_instance_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    _fake_lookup(monkeypatch, SimpleNamespace(status="online", enabled=True), calls)
    owner = _owner()

    assert await bot_connection.BotConnection._instance_still_active(owner) is True  # type: ignore[arg-type]
    assert not owner._stopped.is_set()
